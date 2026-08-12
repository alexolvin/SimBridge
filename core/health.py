"""Comprehensive health checks for all SimBridge components.

Checks: process alive, Asterisk reachable, dongle registered,
Telegram session valid, peer node reachable, bridge endpoint.

Usage::

    from core.health import HealthChecker, HealthStatus
    checker = HealthChecker(ami_client, cfg)
    status = await checker.check_all()
    print(status.to_dict())  # {"status": "ok", "components": {...}}
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from core.logging_config import get_logger

logger = get_logger("simbridge.health")


@dataclass
class ComponentStatus:
    """Health of one component."""
    name: str
    healthy: bool
    detail: str = ""
    last_check: float = field(init=False, default_factory=time.monotonic)


@dataclass
class HealthStatus:
    """Aggregated health: ok / degraded / critical."""

    components: list[ComponentStatus] = field(default_factory=list)

    def add(self, name: str, healthy: bool, detail: str = "") -> None:
        self.components.append(ComponentStatus(name=name, healthy=healthy, detail=detail))

    @property
    def status(self) -> str:
        if not self.components:
            return "unknown"
        critical_components = {"asterisk", "modem", "agent_process"}
        all_healthy = all(c.healthy for c in self.components)
        any_critical_down = any(
            not c.healthy and c.name in critical_components for c in self.components
        )
        if all_healthy:
            return "ok"
        if any_critical_down:
            return "critical"
        return "degraded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "components": {
                c.name: {"healthy": c.healthy, "detail": c.detail}
                for c in self.components
            },
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        }


class HealthChecker:
    """Run health checks against live components.

    Parameters
    ----------
    ami: AMIClient instance (or None if not available)
    cfg: config dict
    """

    def __init__(self, ami: Optional[Any], cfg: Optional[Any]) -> None:
        self._ami = ami
        self._cfg = cfg

    async def check_asterisk(self) -> ComponentStatus:
        """Check if Asterisk AMI is reachable."""
        if self._ami is None:
            return ComponentStatus("asterisk", healthy=False, detail="no AMI client")
        try:
            status = await self._ami.get_modem_status()
            return ComponentStatus(
                "asterisk", healthy=True, detail=f"AMI connected, dongle={status.get('device', 'unknown')}"
            )
        except ConnectionError:
            return ComponentStatus("asterisk", healthy=False, detail="AMI connection failed")
        except OSError as e:
            return ComponentStatus("asterisk", healthy=False, detail=f"AMI error: {e}")

    async def check_modem(self) -> ComponentStatus:
        """Check if dongle is registered to a network."""
        if self._ami is None:
            return ComponentStatus("modem", healthy=False, detail="no AMI client")
        try:
            status = await self._ami.get_modem_status()
            registered = status.get("registered", False)
            signal = status.get("signal_percent")
            operator = status.get("operator", "unknown")
            if registered:
                return ComponentStatus(
                    "modem", healthy=True,
                    detail=f"registered to {operator}, signal={signal}%"
                )
            else:
                return ComponentStatus(
                    "modem", healthy=False, detail="not registered to any network"
                )
        except ConnectionError:
            return ComponentStatus("modem", healthy=False, detail="cannot query modem — AMI down")

    def check_agent_process(self) -> ComponentStatus:
        """Check if the agent process is alive (trivially true if we can run this)."""
        return ComponentStatus("agent_process", healthy=True, detail="running")

    async def check_peer_node(self) -> ComponentStatus:
        """Check if the peer Telegram node is reachable via HTTP."""
        import httpx

        if not self._cfg:
            return ComponentStatus("peer_node", healthy=False, detail="no config")

        listen = self._cfg.get("userbot_http.listen", "")
        if not listen:
            return ComponentStatus("peer_node", healthy=False, detail="no userbot_http.listen")

        secret_env = self._cfg.get("userbot_http.secret_env", "")
        import os
        secret = os.environ.get(secret_env, "")
        if not secret:
            return ComponentStatus("peer_node", healthy=False, detail="secret not set")

        url = f"http://{listen}/health"
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(
                    url,
                    headers={"x-simbridge-secret": secret},
                )
                if resp.status_code == 200:
                    return ComponentStatus("peer_node", healthy=True, detail="HTTP 200")
                else:
                    return ComponentStatus(
                        "peer_node", healthy=False, detail=f"HTTP {resp.status_code}"
                    )
        except httpx.HTTPError as e:
            return ComponentStatus("peer_node", healthy=False, detail=f"connection failed: {e}")

    async def check_telegram_session(self) -> ComponentStatus:
        """Check if Telegram session is valid (Telethon is connected).

        This is a no-op placeholder — the actual check is wired in the userbot
        via ``client.is_connected`` property. Called from userbot's health endpoint.
        """
        return ComponentStatus(
            "telegram_session", healthy=False, detail="checked on Telegram node only"
        )

    async def check_bridge(self) -> ComponentStatus:
        """Check if the voice bridge endpoint is reachable (SIP port).

        For now, a simple TCP connectivity check to bridge_host:bridge_port.
        """
        if not self._cfg:
            return ComponentStatus("bridge", healthy=False, detail="no config")

        bridge_host = self._cfg.get("voice.bridge_host", "")
        bridge_port = self._cfg.get("voice.bridge_port", 5062)

        if not bridge_host:
            return ComponentStatus("bridge", healthy=False, detail="no bridge_host configured")

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(bridge_host, bridge_port),
                timeout=3.0,
            )
            writer.close()
            await writer.wait_closed()
            return ComponentStatus("bridge", healthy=True, detail=f"TCP {bridge_host}:{bridge_port} open")
        except (OSError, asyncio.TimeoutError) as e:
            return ComponentStatus("bridge", healthy=False, detail=f"unreachable: {e}")

    async def check_all(self) -> HealthStatus:
        """Run all health checks and return aggregated status."""
        status = HealthStatus()

        # Synchronous check (always true if we can run this code)
        status.components.append(self.check_agent_process())

        # Async checks — run concurrently
        async_checks = [
            self.check_asterisk(),
            self.check_modem(),
            self.check_peer_node(),
            self.check_bridge(),
        ]
        results = await asyncio.gather(*async_checks, return_exceptions=True)

        for result in results:
            if isinstance(result, ComponentStatus):
                status.components.append(result)
            elif isinstance(result, Exception):
                status.components.append(
                    ComponentStatus("unknown", healthy=False, detail=str(result))
                )

        # Sort: critical components first, then by name
        status.components.sort(key=lambda c: (not c.healthy, c.name))

        return status
