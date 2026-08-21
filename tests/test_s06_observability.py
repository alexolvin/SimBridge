"""S06.2 tests — observability: logging, metrics, health, alerting, recovery.

Tests: TS06-4 (health endpoint), TS06-5 (alert delivery), TS06-6 (recovery).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from core.logging_config import (
    JSONFormatter,
    setup_logging,
    get_correlation,
    set_correlation,
    get_logger,
)
from core.metrics import MetricsCollector, SMSCounters, CallCounters
from core.health import HealthStatus, ComponentStatus, HealthChecker
from core.alerting import AlertManager, AlertRule
from core.recovery import BackoffReconnector, ModemWatchdog


# =========================================================================
# TS06-OBS-01 — JSON logging
# =========================================================================

class TestJSONLogging:
    """Structured JSON logs with UTC timestamps and correlation IDs."""

    def test_json_formatter_produces_valid_json(self):
        import logging
        fmt = JSONFormatter()
        record = logging.LogRecord(
            name="simbridge.test", level=logging.INFO,
            pathname="", lineno=0, msg="test message",
            args=(), exc_info=None,
        )
        output = fmt.format(record)
        data = json.loads(output)
        assert "ts" in data
        assert data["level"] == "INFO"
        assert data["logger"] == "simbridge.test"
        assert data["msg"] == "test message"
        # UTC timestamp
        assert "+00:00" in data["ts"] or "Z" in data["ts"]

    def test_correlation_id_propagation(self):
        set_correlation("abc-123")
        assert get_correlation() == "abc-123"

    def test_correlation_id_in_json(self):
        import logging
        set_correlation("xyz-789")
        fmt = JSONFormatter()
        record = logging.LogRecord(
            name="simbridge.test", level=logging.INFO,
            pathname="", lineno=0, msg="correlated",
            args=(), exc_info=None,
        )
        output = fmt.format(record)
        data = json.loads(output)
        assert data.get("correlation_id") == "xyz-789"

    def test_setup_logging_json(self):
        """setup_logging with json_format=True should configure a JSONFormatter."""
        import logging
        setup_logging(level="DEBUG", json_format=True)
        root = logging.getLogger()
        assert len(root.handlers) > 0
        handler = root.handlers[0]
        assert isinstance(handler.formatter, JSONFormatter)

    def test_structured_adapter_injects_correlation(self):
        set_correlation("adapter-test")
        adapter = get_logger("test.adapter")
        # The adapter wraps a logger and injects correlation_id
        assert adapter is not None


# =========================================================================
# TS06-OBS-02 — Metrics collector
# =========================================================================

class TestMetricsCollector:
    """SMS/call counters, component state, export."""

    def test_sms_counters(self):
        mc = MetricsCollector()
        mc.sms_sent()
        mc.sms_sent()
        mc.sms_delivered()
        mc.sms_failed()
        mc.sms_incoming()

        all_m = mc.get_all()
        assert all_m["sms"]["sent"] == 2
        assert all_m["sms"]["delivered"] == 1
        assert all_m["sms"]["failed"] == 1
        assert all_m["sms"]["incoming"] == 1
        assert all_m["sms"]["delivery_rate"] == 0.5

    def test_sms_delivery_rate_none_when_no_sends(self):
        mc = MetricsCollector()
        assert mc.get_all()["sms"]["delivery_rate"] is None

    def test_call_counters(self):
        mc = MetricsCollector()
        mc.call_answered("incoming")
        mc.call_answered("outgoing")
        mc.call_rejected("incoming")
        mc.call_voicemail()
        mc.call_timeout("outgoing")
        mc.call_failed()

        all_m = mc.get_all()["calls"]
        assert all_m["incoming_answered"] == 1
        assert all_m["outgoing_answered"] == 1
        assert all_m["incoming_rejected"] == 1
        assert all_m["incoming_voicemail"] == 1
        assert all_m["outgoing_timeout"] == 1
        assert all_m["outgoing_failed"] == 1
        assert all_m["total_answered"] == 2
        assert all_m["total_missed"] == 3  # rejected + outgoing_failed + outgoing_timeout

    def test_component_state(self):
        mc = MetricsCollector()
        mc.set_modem_registered(True)
        mc.set_bridge_reachable(False)
        mc.set_telegram_connected(True)

        comp = mc.get_all()["components"]
        assert comp["modem_registered"] is True
        assert comp["bridge_reachable"] is False
        assert comp["telegram_connected"] is True
        assert comp["modem_last_check_age_s"] is not None

    def test_thread_safety(self):
        """Concurrent increments should not lose counts."""
        mc = MetricsCollector()
        errors = []

        def increment(n):
            try:
                for _ in range(n):
                    mc.sms_sent()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=increment, args=(100,)) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert mc.get_all()["sms"]["sent"] == 1000


# =========================================================================
# TS06-OBS-03 — Health status
# =========================================================================

class TestHealthStatus:
    """Aggregated health: ok / degraded / critical."""

    def test_all_healthy_is_ok(self):
        status = HealthStatus()
        status.add("asterisk", True, "connected")
        status.add("modem", True, "registered")
        assert status.status == "ok"

    def test_critical_component_down(self):
        status = HealthStatus()
        status.add("asterisk", False, "connection refused")
        status.add("modem", False, "offline")
        assert status.status == "critical"

    def test_degraded_non_critical(self):
        status = HealthStatus()
        status.add("peer_node", False, "timeout")
        status.add("bridge", False, "unreachable")
        # Neither peer_node nor bridge is in critical_components
        assert status.status == "degraded"

    def test_to_dict_structure(self):
        status = HealthStatus()
        status.add("asterisk", True, "connected")
        d = status.to_dict()
        assert "status" in d
        assert "components" in d
        assert "timestamp" in d
        assert d["components"]["asterisk"]["healthy"] is True

    def test_empty_is_unknown(self):
        status = HealthStatus()
        assert status.status == "unknown"


# =========================================================================
# TS06-OBS-04 — AlertManager
# =========================================================================

class TestAlertManager:
    """Alert rate-limiting with cooldowns."""

    def test_sends_first_alert(self):
        async def _run():
            sent = []
            async def send_fn(msg):
                sent.append(msg)
            mgr = AlertManager(send_fn=send_fn)
            result = await mgr.alert("dongle_offline", "Dongle not registered")
            assert result is True
            assert len(sent) == 1
            assert "SimBridge" in sent[0]
        asyncio.get_event_loop().run_until_complete(_run())

    def test_suppresses_within_cooldown(self):
        async def _run():
            sent = []
            async def send_fn(msg):
                sent.append(msg)
            mgr = AlertManager(send_fn=send_fn)
            await mgr.alert("dongle_offline", "Dongle offline 1")
            result = await mgr.alert("dongle_offline", "Dongle offline 2")
            assert result is False
            assert len(sent) == 1
        asyncio.get_event_loop().run_until_complete(_run())

    def test_unknown_rule_sends_anyway(self):
        async def _run():
            sent = []
            async def send_fn(msg):
                sent.append(msg)
            mgr = AlertManager(send_fn=send_fn)
            result = await mgr.alert("custom_event", "custom message")
            assert result is True
            assert len(sent) == 1
        asyncio.get_event_loop().run_until_complete(_run())

    def test_alert_rule_cooldown(self):
        rule = AlertRule("test", cooldown_seconds=0.1)
        assert rule.should_send() is True
        assert rule.should_send() is False  # within cooldown
        time.sleep(0.15)
        assert rule.should_send() is True


# =========================================================================
# TS06-OBS-05 — Recovery: BackoffReconnector
# =========================================================================

class TestBackoffReconnector:
    """Exponential backoff reconnector."""

    def test_succeeds_on_first_try(self):
        async def _run():
            call_count = 0
            async def op():
                nonlocal call_count
                call_count += 1
            recon = BackoffReconnector(operation=op, label="test")
            result = await recon.start()
            assert result is True
            assert call_count == 1
        asyncio.get_event_loop().run_until_complete(_run())

    def test_retries_on_failure(self):
        async def _run():
            call_count = 0
            async def op():
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ConnectionError("not yet")
            recon = BackoffReconnector(
                operation=op,
                label="retry-test",
                min_delay=0.01,
                max_delay=0.1,
                max_retries=5,
            )
            result = await recon.start()
            assert result is True
            assert call_count == 3
        asyncio.get_event_loop().run_until_complete(_run())

    def test_gives_up_after_max_retries(self):
        async def _run():
            call_count = 0
            gave_up = False
            async def op():
                nonlocal call_count
                call_count += 1
                raise ConnectionError("always fails")
            async def on_give_up():
                nonlocal gave_up
                gave_up = True
            recon = BackoffReconnector(
                operation=op,
                label="fail-test",
                min_delay=0.01,
                max_delay=0.05,
                max_retries=3,
                on_give_up=on_give_up,
            )
            result = await recon.start()
            assert result is False
            assert call_count == 3
            assert gave_up is True
        asyncio.get_event_loop().run_until_complete(_run())


# =========================================================================
# TS06-OBS-06 — Recovery: ModemWatchdog
# =========================================================================

class TestModemWatchdog:
    """Periodic modem health check with reset on consecutive failures."""

    def test_recovers_after_failure(self):
        async def _run():
            check_count = 0
            reset_count = 0
            async def check_fn():
                nonlocal check_count
                check_count += 1
                return check_count >= 3
            async def reset_fn():
                nonlocal reset_count
                reset_count += 1
            wd = ModemWatchdog(
                check_fn=check_fn, reset_fn=reset_fn,
                label="test-modem", check_interval=0.05, max_resets=2,
            )
            await wd.start()
            await asyncio.sleep(0.25)
            wd.stop()
            assert check_count >= 3
        asyncio.get_event_loop().run_until_complete(_run())

    def test_resets_on_consecutive_failures(self):
        async def _run():
            check_count = 0
            reset_count = 0
            async def check_fn():
                nonlocal check_count
                check_count += 1
                return False
            async def reset_fn():
                nonlocal reset_count
                reset_count += 1
            wd = ModemWatchdog(
                check_fn=check_fn, reset_fn=reset_fn,
                label="stuck-modem", check_interval=0.05, max_resets=2,
            )
            await wd.start()
            await asyncio.sleep(0.3)
            wd.stop()
            assert reset_count >= 1
        asyncio.get_event_loop().run_until_complete(_run())

    def test_boot_grace_and_busy_do_not_reset(self):
        """Production wiring (make_modem_check): before the first poll the
        default OFFLINE state is not a failure (boot grace), and CALL_BUSY
        during an active call is not a failure — neither may trigger a
        reset (a "reset" mid-call would drop the active call)."""
        async def _run():
            from core.modem import SingleModemProvider
            from agent.agent import make_modem_check
            provider = SingleModemProvider(modem_id="gsm", device="gsm")
            check_count = 0
            reset_count = 0
            base_check = make_modem_check(provider, "gsm")

            async def counting_check():
                nonlocal check_count
                check_count += 1
                return await base_check()

            async def reset_fn():
                nonlocal reset_count
                reset_count += 1

            wd = ModemWatchdog(
                check_fn=counting_check, reset_fn=reset_fn,
                label="gsm", check_interval=0.02, max_resets=2,
            )
            await wd.start()
            await asyncio.sleep(0.15)  # several checks, no observation yet
            assert check_count >= 3
            provider.update_state("gsm", registered=True, signal_percent=100)
            provider.set_call_active("gsm", True)  # active call -> CALL_BUSY
            await asyncio.sleep(0.15)  # several checks during the "call"
            wd.stop()
            assert reset_count == 0
        asyncio.get_event_loop().run_until_complete(_run())

    def test_observed_offline_does_reset(self):
        """A real observation (poll answered, device entry gone) is a
        failure: max_resets consecutive broken checks still trigger the
        reset."""
        async def _run():
            from core.modem import SingleModemProvider
            from agent.agent import make_modem_check
            provider = SingleModemProvider(modem_id="gsm", device="gsm")
            provider.mark_offline("gsm")  # observation: device is gone
            check_count = 0
            reset_count = 0
            base_check = make_modem_check(provider, "gsm")

            async def counting_check():
                nonlocal check_count
                check_count += 1
                return await base_check()

            async def reset_fn():
                nonlocal reset_count
                reset_count += 1

            wd = ModemWatchdog(
                check_fn=counting_check, reset_fn=reset_fn,
                label="gsm", check_interval=0.02, max_resets=2,
            )
            await wd.start()
            await asyncio.sleep(0.3)
            wd.stop()
            assert check_count >= 2
            assert reset_count >= 1
        asyncio.get_event_loop().run_until_complete(_run())


# =========================================================================
# TS06-OBS-07 — HealthChecker (no AMI)
# =========================================================================

class TestHealthCheckerNoAMI:
    """HealthChecker behavior when AMI client is not available."""

    def test_no_ami_returns_unhealthy(self):
        async def _run():
            checker = HealthChecker(ami=None, cfg=None)
            status = await checker.check_all()
            # Agent process should be healthy (trivially true)
            agent = [c for c in status.components if c.name == "agent_process"]
            assert len(agent) == 1
            assert agent[0].healthy is True
            # Asterisk and modem should be unhealthy (no AMI)
            asterisk = [c for c in status.components if c.name == "asterisk"]
            assert len(asterisk) == 1
            assert asterisk[0].healthy is False
        asyncio.get_event_loop().run_until_complete(_run())


# =========================================================================
# TS06-OBS-07b — HealthChecker: single shared modem-status fetch
# =========================================================================

class TestHealthCheckerSharedFetch:
    """check_all must fetch the modem status exactly once and share it
    between the asterisk and modem checks. Previously each check made its
    own DongleShowDevices round trip — two concurrent AMI calls for one
    piece of data (duplicate mechanism)."""

    @staticmethod
    def _checker(status=None, exc=None, calls=None):
        class FakeAMI:
            async def get_modem_status(self):
                if calls is not None:
                    calls.append(1)
                if exc is not None:
                    raise exc
                return status
        return HealthChecker(ami=FakeAMI(), cfg=None), (calls if calls is not None else [])

    def test_check_all_fetches_once(self):
        async def _run():
            calls = []
            checker, _ = self._checker(
                status={"device": "gsm", "registered": True,
                        "signal_percent": 80, "operator": "test"},
                calls=calls,
            )
            result = await checker.check_all()
            assert len(calls) == 1
            by_name = {c.name: c for c in result.components}
            assert by_name["asterisk"].healthy is True
            assert by_name["modem"].healthy is True
        asyncio.get_event_loop().run_until_complete(_run())

    def test_check_all_fetch_failure_shared(self):
        async def _run():
            calls = []
            checker, _ = self._checker(exc=ConnectionError("ami down"), calls=calls)
            result = await checker.check_all()
            # One failed fetch is shared — not retried per check.
            assert len(calls) == 1
            by_name = {c.name: c for c in result.components}
            assert by_name["asterisk"].healthy is False
            assert "connection failed" in by_name["asterisk"].detail
            assert by_name["modem"].healthy is False
            assert "AMI down" in by_name["modem"].detail
        asyncio.get_event_loop().run_until_complete(_run())

    def test_individual_checks_still_self_fetch(self):
        async def _run():
            calls = []
            checker, _ = self._checker(
                status={"device": "gsm", "registered": True,
                        "signal_percent": 80, "operator": "test"},
                calls=calls,
            )
            await checker.check_asterisk()
            await checker.check_modem()
            assert len(calls) == 2
        asyncio.get_event_loop().run_until_complete(_run())
