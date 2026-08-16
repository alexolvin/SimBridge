"""Agent-side supervisor — edge-triggered alerts from health checks.

S06.2: the health checker already knows the state of every component
(Asterisk, modem, peer node, bridge); this module turns state CHANGES
into alerts for the master user, without re-querying anything:

- dongle present -> absent        -> ``dongle_offline``
- present, registered -> not      -> ``gsm_registration_lost``
- not registered -> registered    -> ``modem_recovery``
- peer healthy   -> unhealthy     -> ``peer_unreachable``
- peer unhealthy -> healthy       -> ``peer_recovery``

Alerts are edge-triggered (fired on transitions, not every cycle):
the cooldown in AlertManager already deduplicates repeats, but firing
on every 30-second cycle while a fault persists would still spam the
master user if the cooldown ever changes.

Component-state metrics (bridge reachable, peer's telegram_connected)
are refreshed from the same health check here — one mechanism, no
second HTTP call for the peer.

``cycle()`` is a pure async function of its inputs (checker, provider,
metrics, alerts, state dict), so it is unit-testable without an app.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from core.health import HealthStatus
from core.modem import ONLINE_STATES, ModemState

logger = logging.getLogger("simbridge.supervisor")


def new_state() -> dict:
    """Initial supervisor state. ``None`` = not observed yet.

    The first observation never alerts: we cannot distinguish
    "the device just came online" from "it has always been online",
    and alerting at startup would page the user for a healthy system.
    """
    return {
        "modem_present": None,
        "modem_registered": None,
        "peer_healthy": None,
    }


async def cycle(
    checker,
    provider,
    modem_id: str,
    metrics,
    alerts,
    state: dict,
) -> None:
    """Run one supervisor cycle: health check, refresh metrics, fire
    edge-triggered alerts, update *state* in place.

    Must not raise: a crashed cycle must not kill the supervisor loop
    (the caller still guards, but this is defense in depth).
    """
    try:
        status: HealthStatus = await checker.check_all()
    except Exception:
        logger.exception("supervisor: health check failed — skipping cycle")
        return

    by_name = {c.name: c for c in status.components}

    # Refresh component metrics from this check (one mechanism).
    bridge = by_name.get("bridge")
    if bridge is not None and metrics is not None:
        metrics.set_bridge_reachable(bridge.healthy)
    peer = by_name.get("peer_node")
    if peer is not None and peer.data is not None and metrics is not None:
        tg = peer.data.get("telegram_connected")
        if isinstance(tg, bool):
            metrics.set_telegram_connected(tg)

    # Modem edges.
    info = provider.get_info(modem_id)
    now_state = info.state if info is not None else ModemState.OFFLINE
    present = now_state in ONLINE_STATES
    registered = bool(info.registered) if info is not None else False

    if state["modem_present"] is not None and present != state["modem_present"]:
        if not present:
            await alerts.alert(
                "dongle_offline",
                f"Dongle {modem_id}: device not present "
                f"(state={now_state.value})",
            )
    if (
        state["modem_registered"] is not None
        and present
        and registered != state["modem_registered"]
    ):
        if not registered:
            await alerts.alert(
                "gsm_registration_lost",
                f"Dongle {modem_id}: lost network registration",
            )
    # Recovery is observed independently of the presence edge: a
    # modem can go OFFLINE -> INITIALIZING -> READY and the recovery
    # alert belongs to the registered edge, not the presence edge.
    if (
        state["modem_registered"] is not None
        and not state["modem_registered"]
        and registered
    ):
        await alerts.alert(
            "modem_recovery",
            f"Dongle {modem_id}: registered again",
        )

    state["modem_present"] = present
    state["modem_registered"] = registered

    # Peer edges.
    if peer is not None:
        if (
            state["peer_healthy"] is not None
            and peer.healthy != state["peer_healthy"]
        ):
            if peer.healthy:
                await alerts.alert(
                    "peer_recovery",
                    "Userbot node reachable again",
                )
            else:
                await alerts.alert(
                    "peer_unreachable",
                    f"Userbot node unreachable: {peer.detail}",
                )
        state["peer_healthy"] = peer.healthy


async def run_supervisor(
    checker,
    provider,
    modem_id: str,
    metrics,
    alerts,
    interval: float,
    stop: Optional[asyncio.Event] = None,
) -> None:
    """Run supervisor cycles every *interval* seconds until *stop* is set.

    The first cycle runs immediately (same contract as the modem
    poller). Crashes are logged and the loop continues — the supervisor
    must outlive any single faulty health check.
    """
    if stop is None:
        stop = asyncio.Event()
    state = new_state()
    while not stop.is_set():
        try:
            await cycle(checker, provider, modem_id, metrics, alerts, state)
        except Exception:  # noqa: BLE001 — a cycle must never kill the loop
            logger.exception("supervisor cycle crashed — retrying")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
