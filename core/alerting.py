"""Alerting — sends critical-event notifications to the master Telegram account.

Alerts for:
- dongle offline / GSM registration lost
- Telegram session invalidated
- peer node unreachable (repeated)
- repeated call failures

Each alert is rate-limited: same alert type is suppressed for ``cooldown_seconds``
to avoid flooding the master account.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from core.logging_config import get_logger

logger = get_logger("simbridge.alerting")


@dataclass
class AlertRule:
    """One alert rule: name, cooldown, and a callback to send the notification."""

    name: str
    cooldown_seconds: int = 300  # 5 minutes default
    _last_sent: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def should_send(self) -> bool:
        """Return True if enough time has passed since the last alert."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_sent >= self.cooldown_seconds:
                self._last_sent = now
                return True
            return False


class AlertManager:
    """Manage alert rules and send notifications via a pluggable transport.

    Usage::

        async def send(msg):
            await client.send_message(master_id, msg)

        alerts = AlertManager(send_fn=send)
        await alerts.alert("dongle_offline", "Dongle gsm: not registered")
    """

    def __init__(self, send_fn) -> None:
        self._send_fn = send_fn
        self._lock = threading.Lock()
        self._rules: dict[str, AlertRule] = {}

        # Pre-registered rules
        self._register_default_rules()

    def _register_default_rules(self) -> None:
        """Register default alert rules with cooldowns."""
        defaults = {
            "dongle_offline": AlertRule("dongle_offline", cooldown_seconds=300),
            "gsm_registration_lost": AlertRule("gsm_registration_lost", cooldown_seconds=300),
            "telegram_session_invalid": AlertRule("telegram_session_invalid", cooldown_seconds=600),
            "peer_unreachable": AlertRule("peer_unreachable", cooldown_seconds=300),
            "repeated_call_failures": AlertRule("repeated_call_failures", cooldown_seconds=600),
            "modem_recovery": AlertRule("modem_recovery", cooldown_seconds=600),
            "peer_recovery": AlertRule("peer_recovery", cooldown_seconds=600),
        }
        with self._lock:
            self._rules.update(defaults)

    def register_rule(self, name: str, cooldown_seconds: int = 300) -> None:
        """Register a custom alert rule."""
        with self._lock:
            self._rules[name] = AlertRule(name, cooldown_seconds=cooldown_seconds)

    async def alert(self, rule_name: str, message: str) -> bool:
        """Send an alert if cooldown has elapsed.

        Returns True if the alert was sent, False if suppressed by cooldown.
        """
        with self._lock:
            rule = self._rules.get(rule_name)

        if rule is None:
            # Unknown rule — send anyway (fail-open)
            logger.warning("Alerting on unknown rule: %s", rule_name)
            await self._send_fn(message)
            return True

        if not rule.should_send():
            logger.debug("Alert suppressed (cooldown): %s", rule_name)
            return False

        formatted = f"🚨 SimBridge: {message}"
        logger.info("Sending alert [%s]: %s", rule_name, message)

        try:
            await self._send_fn(formatted)
            return True
        except Exception as e:
            logger.error("Failed to send alert [%s]: %s", rule_name, e)
            return False

    @property
    def rules(self) -> dict[str, AlertRule]:
        with self._lock:
            return dict(self._rules)
