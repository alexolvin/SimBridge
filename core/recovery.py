"""Automatic recovery with exponential backoff.

Handles recoverable failures:
- Modem stuck → reset attempt via AMI
- Asterisk AMI disconnected → reconnect with backoff
- Peer node unreachable → retry with backoff

Each recovery loop logs attempts and escalates to alerting after
``max_retries`` consecutive failures.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Awaitable, Optional

from core.logging_config import get_logger

logger = get_logger("simbridge.recovery")


class BackoffReconnector:
    """Retry an async operation with exponential backoff.

    Usage::

        recon = BackoffReconnector(
            operation=ami.connect,
            min_delay=1.0,
            max_delay=60.0,
            max_retries=10,
            on_give_up=lambda: alerts.alert("ami_down", "AMI reconnect failed after 10 attempts"),
        )
        await recon.start()  # runs until operation succeeds, then stops
        # To reconnect on future failures:
        await recon.reconnect()
    """

    def __init__(
        self,
        operation: Callable[[], Awaitable[None]],
        label: str = "operation",
        min_delay: float = 1.0,
        max_delay: float = 60.0,
        max_retries: int = 10,
        on_give_up: Optional[Callable[[], Awaitable[None]]] = None,
        on_success: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._operation = operation
        self._label = label
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._max_retries = max_retries
        self._on_give_up = on_give_up
        self._on_success = on_success
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> bool:
        """Start reconnecting. Returns True if the operation succeeded."""
        if self._task and not self._task.done():
            return False  # already running

        self._running = True
        self._task = asyncio.ensure_future(self._retry_loop())
        return await self._task

    async def _retry_loop(self) -> bool:
        """Execute the operation with exponential backoff."""
        delay = self._min_delay
        attempt = 0

        while self._running:
            attempt += 1
            logger.info(
                "Reconnect attempt %d/%d for %s",
                attempt, self._max_retries, self._label,
            )

            try:
                await self._operation()
                logger.info("Successfully reconnected to %s", self._label)
                if self._on_success:
                    try:
                        await self._on_success()
                    except Exception as e:
                        logger.error("on_success callback failed: %s", e)
                self._running = False
                return True
            except Exception as e:
                logger.warning(
                    "%s reconnect failed (attempt %d): %s",
                    self._label, attempt, e,
                )

                if attempt >= self._max_retries:
                    logger.error(
                        "%s: gave up after %d attempts",
                        self._label, self._max_retries,
                    )
                    if self._on_give_up:
                        try:
                            await self._on_give_up()
                        except Exception as e:
                            logger.error("on_give_up callback failed: %s", e)
                    self._running = False
                    return False

                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_delay)

        return False

    def stop(self) -> None:
        """Stop the reconnect loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()


class ModemWatchdog:
    """Periodically check modem state and attempt recovery if stuck.

    Monitors the modem via an AMI client. If the modem is OFFLINE or ERROR
    for more than ``check_interval`` seconds, it attempts a reset.
    After ``max_resets`` consecutive failures, it alerts and stops trying.
    """

    def __init__(
        self,
        check_fn: Callable[[], Awaitable[bool]],
        reset_fn: Callable[[], Awaitable[None]],
        label: str = "modem",
        check_interval: float = 30.0,
        max_resets: int = 3,
        alert_fn: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self._check_fn = check_fn
        self._reset_fn = reset_fn
        self._label = label
        self._check_interval = check_interval
        self._max_resets = max_resets
        self._alert_fn = alert_fn
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._consecutive_failures = 0

    async def start(self) -> None:
        """Start the watchdog loop. Runs until stop() is called."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.ensure_future(self._watch_loop())

    async def _watch_loop(self) -> None:
        while self._running:
            try:
                healthy = await self._check_fn()
                if healthy:
                    if self._consecutive_failures > 0:
                        logger.info(
                            "%s recovered after %d failed checks",
                            self._label, self._consecutive_failures,
                        )
                        if self._alert_fn:
                            await self._alert_fn(
                                f"{self._label} recovered"
                            )
                    self._consecutive_failures = 0
                else:
                    self._consecutive_failures += 1
                    logger.warning(
                        "%s unhealthy (check %d/%d)",
                        self._label,
                        self._consecutive_failures,
                        self._max_resets,
                    )

                    if self._consecutive_failures >= self._max_resets:
                        logger.error(
                            "%s: attempting reset after %d consecutive failures",
                            self._label, self._max_resets,
                        )
                        try:
                            await self._reset_fn()
                            logger.info("%s: reset sent", self._label)
                            self._consecutive_failures = 0
                        except Exception as e:
                            logger.error("%s: reset failed: %s", self._label, e)
                            if self._alert_fn:
                                await self._alert_fn(
                                    f"{self._label} stuck — reset failed: {e}"
                                )
            except Exception as e:
                logger.error("%s: check error: %s", self._label, e)

            await asyncio.sleep(self._check_interval)

    def stop(self) -> None:
        """Stop the watchdog loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
