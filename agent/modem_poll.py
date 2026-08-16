"""Periodic modem-state poller — feeds real device state into the provider.

S05.1: modem state must be derived from the real device (``DongleShowDevices``
via AMI), not tracked optimistically in memory where it can drift from
reality. This module is the single place where the two meet: the agent's
lifespan runs :func:`run_modem_poller` as a background task.

Without this, a ``SingleModemProvider`` stays OFFLINE forever (nobody
else calls ``update_state``), the pool never sees a READY modem, and
every outgoing call fails with 503.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.modem import ModemProvider

logger = logging.getLogger("simbridge.modem_poll")


async def poll_modem_state(
    ami,
    provider: "ModemProvider",
    modem_id: str,
    metrics=None,
) -> bool:
    """Run one poll cycle: query the device, feed the provider.

    Returns True if the modem is currently available (READY).

    Device present  -> registration/signal/operator are updated.
    Device absent   -> ``mark_offline()`` (unplugged / driver reset).
    AMI unreachable -> the last known state is kept and logged; the poll
                       retried next cycle. State is only as stale as one
                       poll interval, never fabricated (and the
                       ``modem_registered`` metric is NOT updated either —
                       an AMI outage is not evidence of deregistration).
    """
    try:
        status = await ami.get_modem_status()
    except (ConnectionError, OSError):
        logger.warning("modem state poll failed — keeping last known state")
        return provider.is_available(modem_id)
    if not status:
        # No DongleDeviceEntry for this device — it is gone.
        provider.mark_offline(modem_id)
        if metrics is not None:
            metrics.set_modem_registered(False)
        return False
    provider.update_state(
        modem_id,
        registered=status.get("registered", False),
        signal_percent=status.get("signal_percent"),
        operator=status.get("operator"),
    )
    if metrics is not None:
        metrics.set_modem_registered(status.get("registered", False))
    return provider.is_available(modem_id)


async def run_modem_poller(
    ami,
    provider: "ModemProvider",
    modem_id: str,
    interval: float,
    stop: Optional[asyncio.Event] = None,
    metrics=None,
) -> None:
    """Poll the device every *interval* seconds until *stop* is set.

    The first poll runs immediately, so the provider reaches READY as
    soon as the device answers instead of one interval after startup.
    """
    if stop is None:
        stop = asyncio.Event()
    while not stop.is_set():
        try:
            await poll_modem_state(ami, provider, modem_id, metrics=metrics)
        except Exception:  # noqa: BLE001 — a poll must never kill the loop
            logger.exception("modem state poll cycle crashed — retrying")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
