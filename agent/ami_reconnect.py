"""S06.3: AMI auto-reconnect wiring.

AMIClient has no internal reconnect: once the AMI connection drops, every
subsequent call raises ``ConnectionError`` until ``connect()`` is called
again — without this wrapper the agent would stay broken until a manual
restart.

``AMIReconnect`` is a forwarding wrapper: any ``ConnectionError`` from any
caller (a request, the modem poller, a health check) kicks the shared
``BackoffReconnector``, which retries with backoff and alerts on give-up.
``start()`` is a no-op while a reconnect is already in flight, so kicking
on every error is safe and idempotent.

One wrapper, one kick point — instead of a reconnect trigger scattered
over every AMI call site in the routes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.logging_config import get_logger

logger = get_logger("simbridge.ami_reconnect")


class AMIReconnect:
    """Forwarding wrapper around an AMIClient with auto-reconnect kick.

    All attribute access is forwarded to the wrapped client; callable
    attributes are wrapped so a raised ``ConnectionError`` triggers the
    reconnector before propagating to the caller (which still sees it).
    """

    def __init__(self, ami, reconnector) -> None:
        self._ami = ami
        self._reconnector = reconnector
        self._reconnect_task = None

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._ami, name)
        if not callable(attr):
            return attr

        async def wrapped(*args, **kwargs):
            try:
                return await attr(*args, **kwargs)
            except ConnectionError:
                self._kick()
                raise

        return wrapped

    def _kick(self) -> None:
        """Start (or ignore an already-running) reconnect attempt."""
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return  # reconnect already in flight
        logger.info("AMI connection lost — starting backoff reconnect")
        self._reconnect_task = asyncio.create_task(self._reconnector.start())
