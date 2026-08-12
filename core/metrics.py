"""Metrics collector — SMS in/out, delivery rate, call counts, modem state.

Thread-safe counters. Designed for periodic export (e.g., health endpoint,
log lines, or future Prometheus integration).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SMSCounters:
    """SMS throughput counters."""
    sent: int = 0
    delivered: int = 0
    failed: int = 0
    incoming: int = 0

    @property
    def delivery_rate(self) -> Optional[float]:
        """Return delivery rate as a fraction (0.0–1.0). None if no sends."""
        if self.sent == 0:
            return None
        return self.delivered / self.sent


@dataclass
class CallCounters:
    """Call outcome counters."""
    incoming_answered: int = 0
    incoming_rejected: int = 0
    incoming_voicemail: int = 0
    incoming_timeout: int = 0
    outgoing_answered: int = 0
    outgoing_failed: int = 0
    outgoing_timeout: int = 0

    @property
    def total_answered(self) -> int:
        return self.incoming_answered + self.outgoing_answered

    @property
    def total_missed(self) -> int:
        return (
            self.incoming_rejected
            + self.incoming_timeout
            + self.outgoing_failed
            + self.outgoing_timeout
        )


class MetricsCollector:
    """Thread-safe global metrics store.

    Usage::

        metrics = MetricsCollector()
        metrics.sms_sent()
        metrics.sms_delivered()
        metrics.get_all()  # dict for health endpoint export
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sms = SMSCounters()
        self._calls = CallCounters()
        self._modem_registered: Optional[bool] = None
        self._modem_last_check: float = 0.0
        self._bridge_reachable: Optional[bool] = None
        self._bridge_last_check: float = 0.0
        self._telegram_connected: Optional[bool] = None
        self._telegram_last_check: float = 0.0

    # --- SMS ---

    def sms_sent(self) -> None:
        with self._lock:
            self._sms.sent += 1

    def sms_delivered(self) -> None:
        with self._lock:
            self._sms.delivered += 1

    def sms_failed(self) -> None:
        with self._lock:
            self._sms.failed += 1

    def sms_incoming(self) -> None:
        with self._lock:
            self._sms.incoming += 1

    # --- Calls ---

    def call_answered(self, direction: str) -> None:
        with self._lock:
            if direction == "incoming":
                self._calls.incoming_answered += 1
            else:
                self._calls.outgoing_answered += 1

    def call_rejected(self, direction: str) -> None:
        with self._lock:
            if direction == "incoming":
                self._calls.incoming_rejected += 1

    def call_voicemail(self) -> None:
        with self._lock:
            self._calls.incoming_voicemail += 1

    def call_timeout(self, direction: str) -> None:
        with self._lock:
            if direction == "incoming":
                self._calls.incoming_timeout += 1
            else:
                self._calls.outgoing_timeout += 1

    def call_failed(self) -> None:
        with self._lock:
            self._calls.outgoing_failed += 1

    # --- Component state ---

    def set_modem_registered(self, registered: bool) -> None:
        with self._lock:
            self._modem_registered = registered
            self._modem_last_check = time.monotonic()

    def set_bridge_reachable(self, reachable: bool) -> None:
        with self._lock:
            self._bridge_reachable = reachable
            self._bridge_last_check = time.monotonic()

    def set_telegram_connected(self, connected: bool) -> None:
        with self._lock:
            self._telegram_connected = connected
            self._telegram_last_check = time.monotonic()

    # --- Export ---

    def get_all(self) -> dict:
        """Return a flat dict of all metrics for health endpoint export."""
        with self._lock:
            return {
                "sms": {
                    "sent": self._sms.sent,
                    "delivered": self._sms.delivered,
                    "failed": self._sms.failed,
                    "incoming": self._sms.incoming,
                    "delivery_rate": self._sms.delivery_rate,
                },
                "calls": {
                    "incoming_answered": self._calls.incoming_answered,
                    "incoming_rejected": self._calls.incoming_rejected,
                    "incoming_voicemail": self._calls.incoming_voicemail,
                    "incoming_timeout": self._calls.incoming_timeout,
                    "outgoing_answered": self._calls.outgoing_answered,
                    "outgoing_failed": self._calls.outgoing_failed,
                    "outgoing_timeout": self._calls.outgoing_timeout,
                    "total_answered": self._calls.total_answered,
                    "total_missed": self._calls.total_missed,
                },
                "components": {
                    "modem_registered": self._modem_registered,
                    "modem_last_check_age_s": (
                        round(time.monotonic() - self._modem_last_check, 1)
                        if self._modem_last_check
                        else None
                    ),
                    "bridge_reachable": self._bridge_reachable,
                    "bridge_last_check_age_s": (
                        round(time.monotonic() - self._bridge_last_check, 1)
                        if self._bridge_last_check
                        else None
                    ),
                    "telegram_connected": self._telegram_connected,
                    "telegram_last_check_age_s": (
                        round(time.monotonic() - self._telegram_last_check, 1)
                        if self._telegram_last_check
                        else None
                    ),
                },
            }
