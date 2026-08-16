"""S06 wiring tests — observability and recovery glue (S06.1–S06.3).

Complements test_s06_observability.py (core primitives: MetricsCollector,
HealthChecker, AlertManager, BackoffReconnector, ModemWatchdog) by pinning
the WIRING — where each component is actually connected and what flows
through it:

- PJSIP bind: distributed mode binds the Tailscale IP, wildcard binds are
  refused (S06.1: no 0.0.0.0 listener);
- supervisor cycle: edge-triggered alerts from health checks;
- modem_alert_rule: watchdog message -> alert rule mapping;
- userbot /events/alert: agent alerts forwarded to the master (S06.2);
- userbot /health: Telegram session state + metrics (S06.2);
- userbot correlation middleware (S06.2);
- call metrics via the CallRegistry transition hook (S06.2);
- SMS route metrics: sent / failed / delivered (S06.2);
- modem poller: device state -> provider + modem_registered metric;
- userbot run_with_recovery: backoff reconnect on session drop (S06.2).

NOTE: asyncio.run() is not used in this file — on Python 3.9 it resets the
global event-loop policy on exit, which breaks other tests in the suite.
The _run() helper below uses a fresh loop that is closed after each use.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import deps
from agent.agent import modem_alert_rule
from agent.modem_poll import poll_modem_state
from agent.routes import router
from agent.supervisor import cycle, new_state
from core.acl import ACLManager
from core.call_control import CallRegistry
from core.events import EventType
from core.health import ComponentStatus, HealthStatus
from core.metrics import MetricsCollector
from core.modem import ModemState, SingleModemProvider
from core.ratelimit import RateLimiter
from core.sms_correlation import SMSCorrelationStore
from scripts.generate_asterisk_config import generate_pjsip
from userbot.http_server import create_http_server


def _run(coro):
    """Run a coroutine on a fresh loop, then close it (see module note)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


SECRET = "s06-sec"


class FakeAudit:
    """Duck-typed AuditLogger: records (event_type, kwargs) pairs."""

    def __init__(self):
        self.calls = []

    def log(self, event_type, **kw):
        self.calls.append((event_type, kw))


class FakeTgClient:
    """Duck-typed Telethon client: records sent messages."""

    def __init__(self, fail_for=frozenset()):
        self.sent = []
        self._fail_for = set(fail_for)

    async def send_message(self, uid, text):
        if uid in self._fail_for:
            raise RuntimeError(f"fake failure for {uid}")
        self.sent.append((uid, text))


def _make_userbot_app(tmp_path, client=None, master_id=None, metrics=None,
                      allowed_peers=None):
    """Build the userbot HTTP app with test doubles. Returns (app, audit)."""
    acl_file = tmp_path / "acl.conf"
    acl_file.write_text("111 in_sms\n")
    audit = FakeAudit()
    app = create_http_server(
        secret=SECRET,
        allowed_peers=allowed_peers if allowed_peers is not None else [],
        acl=ACLManager(str(acl_file)),
        audit=audit,
        client=client,
        master_id=master_id,
        metrics=metrics,
    )
    return app, audit


# ---------------------------------------------------------------------------
# S06.1: PJSIP bind — no wildcard listener
# ---------------------------------------------------------------------------

class TestPjsipBind:
    DIST = {"voice": {"bridge_endpoint": "tg-bridge",
                      "bridge_host": "100.70.1.2",
                      "bridge_port": 5062}}

    def _gen(self, tmp_path, cfg, node_ip=""):
        out = tmp_path / "pjsip.conf"
        generate_pjsip(cfg, str(out),
                       bridge_secret="s06-bridge-secret", node_ip=node_ip)
        return out.read_text()

    def test_distributed_binds_tailscale_ip(self, tmp_path):
        text = self._gen(tmp_path, self.DIST, node_ip="100.70.1.9")
        assert "bind=100.70.1.9" in text
        assert "0.0.0.0" not in text
        assert "external_media_addr=100.70.1.9" in text

    def test_distributed_without_node_ip_refused(self, tmp_path):
        with pytest.raises(SystemExit) as ei:
            self._gen(tmp_path, self.DIST, node_ip="")
        assert ei.value.code == 1

    def test_single_node_binds_loopback(self, tmp_path):
        cfg = {"voice": {"bridge_endpoint": "tg-bridge",
                         "bridge_host": "127.0.0.1",
                         "bridge_port": 5062}}
        text = self._gen(tmp_path, cfg)
        assert "bind=127.0.0.1" in text
        assert "external_media_addr" not in text


# ---------------------------------------------------------------------------
# S06.2: supervisor cycle — edge-triggered alerts
# ---------------------------------------------------------------------------

class _FakeChecker:
    def __init__(self):
        self.comps = []
        self.raise_exc = None

    def set_components(self, *comps):
        self.comps = list(comps)

    async def check_all(self):
        if self.raise_exc is not None:
            raise self.raise_exc
        return HealthStatus(components=self.comps)


class _FakeAlerts:
    def __init__(self):
        self.calls = []

    async def alert(self, rule, message):
        self.calls.append((rule, message))
        return True


class _RecMetrics:
    """Records the component-state setters the supervisor calls."""

    def __init__(self):
        self.bridge_reachable = "unset"
        self.telegram_connected = "unset"
        self.modem_registered = "unset"

    def set_bridge_reachable(self, v):
        self.bridge_reachable = v

    def set_telegram_connected(self, v):
        self.telegram_connected = v

    def set_modem_registered(self, v):
        self.modem_registered = v


def _comps(peer_healthy=True, bridge_healthy=True, tg_connected=True):
    return [
        ComponentStatus(name="bridge", healthy=bridge_healthy),
        ComponentStatus(name="peer_node", healthy=peer_healthy,
                        detail="" if peer_healthy else "connection timeout",
                        data={"telegram_connected": tg_connected}),
    ]


class TestSupervisorCycle:
    def _env(self):
        checker = _FakeChecker()
        provider = SingleModemProvider(modem_id="gsm")
        metrics = _RecMetrics()
        alerts = _FakeAlerts()
        state = new_state()
        return checker, provider, metrics, alerts, state

    @staticmethod
    def _cycle(checker, provider, metrics, alerts, state):
        _run(cycle(checker, provider, "gsm", metrics, alerts, state))

    def _ready(self, provider):
        provider.update_state("gsm", registered=True, signal_percent=80)

    def test_first_observation_never_alerts(self):
        checker, provider, metrics, alerts, state = self._env()
        self._ready(provider)
        checker.set_components(*_comps())
        self._cycle(checker, provider, metrics, alerts, state)
        assert alerts.calls == []
        assert state["modem_present"] is True
        assert state["modem_registered"] is True
        assert state["peer_healthy"] is True
        assert metrics.bridge_reachable is True
        assert metrics.telegram_connected is True

    def test_dongle_absent_alerts_once(self):
        checker, provider, metrics, alerts, state = self._env()
        self._ready(provider)
        checker.set_components(*_comps())
        self._cycle(checker, provider, metrics, alerts, state)
        provider.mark_offline("gsm")
        self._cycle(checker, provider, metrics, alerts, state)
        assert [c[0] for c in alerts.calls] == ["dongle_offline"]
        # Fault persists: edge-triggered, no repeat.
        self._cycle(checker, provider, metrics, alerts, state)
        assert len(alerts.calls) == 1

    def test_registration_lost_alerts(self):
        checker, provider, metrics, alerts, state = self._env()
        self._ready(provider)
        checker.set_components(*_comps())
        self._cycle(checker, provider, metrics, alerts, state)
        # Present (INITIALIZING) but deregistered.
        provider.update_state("gsm", registered=False, signal_percent=50)
        self._cycle(checker, provider, metrics, alerts, state)
        assert [c[0] for c in alerts.calls] == ["gsm_registration_lost"]

    def test_recovery_after_offline_alerts_modem_recovery(self):
        checker, provider, metrics, alerts, state = self._env()
        self._ready(provider)
        checker.set_components(*_comps())
        self._cycle(checker, provider, metrics, alerts, state)
        provider.mark_offline("gsm")
        self._cycle(checker, provider, metrics, alerts, state)
        self._ready(provider)
        self._cycle(checker, provider, metrics, alerts, state)
        assert [c[0] for c in alerts.calls] == ["dongle_offline",
                                                "modem_recovery"]

    def test_call_busy_is_not_an_edge(self):
        checker, provider, metrics, alerts, state = self._env()
        self._ready(provider)
        checker.set_components(*_comps())
        self._cycle(checker, provider, metrics, alerts, state)
        provider.set_call_active("gsm", True)  # CALL_BUSY, still registered
        self._cycle(checker, provider, metrics, alerts, state)
        assert alerts.calls == []
        assert state["modem_present"] is True
        assert state["modem_registered"] is True

    def test_peer_down_and_recovery(self):
        checker, provider, metrics, alerts, state = self._env()
        self._ready(provider)
        checker.set_components(*_comps())
        self._cycle(checker, provider, metrics, alerts, state)
        checker.set_components(*_comps(peer_healthy=False))
        self._cycle(checker, provider, metrics, alerts, state)
        checker.set_components(*_comps(peer_healthy=True))
        self._cycle(checker, provider, metrics, alerts, state)
        assert [c[0] for c in alerts.calls] == ["peer_unreachable",
                                                "peer_recovery"]

    def test_health_check_crash_skips_cycle(self):
        checker, provider, metrics, alerts, state = self._env()
        checker.raise_exc = RuntimeError("boom")
        self._cycle(checker, provider, metrics, alerts, state)
        assert alerts.calls == []
        assert state["modem_present"] is None
        assert state["modem_registered"] is None
        assert state["peer_healthy"] is None


class TestModemAlertRule:
    def test_recovered_maps_to_modem_recovery(self):
        assert modem_alert_rule("gsm recovered") == "modem_recovery"

    def test_stuck_maps_to_dongle_offline(self):
        assert (modem_alert_rule("gsm stuck — reset failed: not supported")
                == "dongle_offline")


# ---------------------------------------------------------------------------
# S06.2: userbot /events/alert — agent alerts forwarded to the master
# ---------------------------------------------------------------------------

class TestEventsAlert:
    @staticmethod
    def _post(client, message, secret=SECRET):
        return client.post("/events/alert",
                           json={"message": message},
                           headers={"X-SimBridge-Secret": secret})

    def test_forwarded_to_master(self, tmp_path):
        tg = FakeTgClient()
        app, audit = _make_userbot_app(tmp_path, client=tg, master_id=777)
        r = self._post(TestClient(app), "Dongle gsm: device not present")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "sent": True}
        assert tg.sent == [(777, "Dongle gsm: device not present")]
        etype, kw = audit.calls[0]
        assert etype == EventType.ALERT_SENT
        assert kw["telegram_user_id"] == 777
        assert kw["outcome"] == "ok"
        assert kw["details"]["sent"] is True

    def test_wrong_secret_401(self, tmp_path):
        app, _ = _make_userbot_app(tmp_path, client=FakeTgClient(),
                                   master_id=777)
        assert self._post(TestClient(app), "x",
                          secret="wrong").status_code == 401

    def test_ip_not_allowed_403(self, tmp_path):
        app, _ = _make_userbot_app(tmp_path, client=FakeTgClient(),
                                   master_id=777,
                                   allowed_peers=["10.9.9.9"])
        # TestClient connects from "testclient" — not in the allow-list.
        assert self._post(TestClient(app), "x").status_code == 403

    def test_empty_message_400(self, tmp_path):
        app, _ = _make_userbot_app(tmp_path, client=FakeTgClient(),
                                   master_id=777)
        assert self._post(TestClient(app), "   ").status_code == 400

    def test_no_master_not_sent(self, tmp_path):
        tg = FakeTgClient()
        app, audit = _make_userbot_app(tmp_path, client=tg)  # master_id=None
        r = self._post(TestClient(app), "boom")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "sent": False}
        assert tg.sent == []
        assert audit.calls[0][1]["outcome"] == "failed"

    def test_send_failure_still_200(self, tmp_path):
        tg = FakeTgClient(fail_for={777})
        app, audit = _make_userbot_app(tmp_path, client=tg, master_id=777)
        r = self._post(TestClient(app), "boom")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "sent": False}
        assert audit.calls[0][1]["outcome"] == "failed"

    def test_preview_truncated_to_200_chars(self, tmp_path):
        tg = FakeTgClient()
        app, audit = _make_userbot_app(tmp_path, client=tg, master_id=777)
        self._post(TestClient(app), "x" * 300)
        assert len(audit.calls[0][1]["details"]["message_preview"]) == 200


# ---------------------------------------------------------------------------
# S06.2: userbot /health — session state + metrics
# ---------------------------------------------------------------------------

class _ConnClient(FakeTgClient):
    """Adds the is_connected property the /health endpoint reads."""

    def __init__(self, connected=True, raise_exc=False):
        super().__init__()
        self._connected = connected
        self._raise_exc = raise_exc

    @property
    def is_connected(self):
        if self._raise_exc:
            raise RuntimeError("no running loop")
        return self._connected


class TestUserbotHealth:
    def test_no_client(self, tmp_path):
        app, _ = _make_userbot_app(tmp_path)
        body = TestClient(app).get("/health").json()
        assert body["status"] == "ok"
        assert body["telegram_connected"] is None
        assert body["metrics"] is None

    def test_connected_ok_with_metrics(self, tmp_path):
        metrics = MetricsCollector()
        app, _ = _make_userbot_app(tmp_path, client=_ConnClient(True),
                                   metrics=metrics)
        body = TestClient(app).get("/health").json()
        assert body["status"] == "ok"
        assert body["telegram_connected"] is True
        assert body["metrics"]["sms"]["incoming"] == 0
        assert "timestamp" in body

    def test_disconnected_degraded(self, tmp_path):
        app, _ = _make_userbot_app(tmp_path, client=_ConnClient(False))
        body = TestClient(app).get("/health").json()
        assert body["status"] == "degraded"
        assert body["telegram_connected"] is False

    def test_is_connected_raises_treated_disconnected(self, tmp_path):
        app, _ = _make_userbot_app(tmp_path,
                                   client=_ConnClient(raise_exc=True))
        body = TestClient(app).get("/health").json()
        assert body["telegram_connected"] is False
        assert body["status"] == "degraded"


# ---------------------------------------------------------------------------
# S06.2: userbot correlation middleware
# ---------------------------------------------------------------------------

class TestUserbotCorrelation:
    def test_inbound_id_echoed(self, tmp_path):
        app, _ = _make_userbot_app(tmp_path)
        r = TestClient(app).get("/health",
                                headers={"x-correlation-id": "abc123"})
        assert r.headers["x-correlation-id"] == "abc123"

    def test_fresh_id_minted_when_absent(self, tmp_path):
        app, _ = _make_userbot_app(tmp_path)
        r = TestClient(app).get("/health")
        assert len(r.headers["x-correlation-id"]) == 32  # uuid4().hex


# ---------------------------------------------------------------------------
# S06.2: call metrics via the CallRegistry transition hook
# ---------------------------------------------------------------------------

class _CallMetrics:
    def __init__(self):
        self.answered = []
        self.rejected = []
        self.voicemail = 0
        self.timeouts = []
        self.failed = 0
        self.durations = []

    def call_answered(self, direction):
        self.answered.append(direction)

    def call_rejected(self, direction):
        self.rejected.append(direction)

    def call_voicemail(self):
        self.voicemail += 1

    def call_timeout(self, direction):
        self.timeouts.append(direction)

    def call_failed(self):
        self.failed += 1

    def record_answered_duration(self, seconds):
        self.durations.append(seconds)


class TestCallMetricsWiring:
    def _registry(self):
        metrics = _CallMetrics()
        registry = CallRegistry(sms_store=MagicMock(), audit=MagicMock(),
                                metrics=metrics)
        return registry, metrics

    def test_incoming_answered_counted_once_with_duration(self):
        registry, m = self._registry()
        call = registry.create_incoming(caller_number="+79261234555")
        registry.start_telegram_ring(call.call_id)
        registry.accept_incoming(call.call_id)
        registry.answer_gsm(call.call_id)
        registry.bridge_call(call.call_id)
        # Pin the BRIDGED timestamp so the duration is deterministic.
        call.bridged_at = (datetime.now(timezone.utc)
                           - timedelta(seconds=60)).isoformat()
        assert registry.hangup(call.call_id, reason="user_hangup") is True
        assert m.answered == ["incoming"]
        assert len(m.durations) == 1
        assert 59.0 <= m.durations[0] <= 61.0

    def test_incoming_rejected(self):
        registry, m = self._registry()
        call = registry.create_incoming(caller_number="+79261234555")
        assert registry.reject(call.call_id) is True
        assert m.rejected == ["incoming"]

    def test_incoming_voicemail(self):
        registry, m = self._registry()
        call = registry.create_incoming(caller_number="+79261234555")
        assert registry.fallback_to_voicemail(call.call_id) is True
        assert m.voicemail == 1

    def test_outgoing_gsm_busy_counts_failed(self):
        registry, m = self._registry()
        call = registry.create_outgoing(callee_number="+14155552671")
        registry.start_telegram_calling(call.call_id)
        registry.user_accepted(call.call_id)
        registry.dial_gsm(call.call_id)
        assert registry.gsm_busy(call.call_id) is True
        assert m.failed == 1

    def test_outgoing_telegram_timeout(self):
        registry, m = self._registry()
        call = registry.create_outgoing(callee_number="+14155552671")
        registry.start_telegram_calling(call.call_id)
        assert registry.telegram_timeout(call.call_id) is True
        assert m.timeouts == ["outgoing"]

    def test_without_metrics_still_works(self):
        registry = CallRegistry(sms_store=MagicMock(), audit=MagicMock())
        call = registry.create_incoming(caller_number="+79261234555")
        registry.start_telegram_ring(call.call_id)
        registry.accept_incoming(call.call_id)
        registry.answer_gsm(call.call_id)
        registry.bridge_call(call.call_id)
        assert registry.hangup(call.call_id) is True


# ---------------------------------------------------------------------------
# S06.2: SMS route metrics (sent / failed / delivered)
# ---------------------------------------------------------------------------

class _FakeAMI:
    def __init__(self, exc=None):
        self._exc = exc
        self.sent = []

    async def send_sms(self, to, text):
        if self._exc is not None:
            raise self._exc
        self.sent.append((to, text))


class _NoBlacklist:
    def contains(self, number):
        return False


class _AllBlacklist:
    def contains(self, number):
        return True


@pytest.fixture()
def sms_env(tmp_path):
    """Agent router with test doubles on app.state. Yields (client, metrics,
    state). Monkeypatches the router-level auth (TestClient peer is
    "testclient")."""
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    metrics = MetricsCollector()
    app.state.cfg = {
        "asterisk.dongle": "gsm",
        "limits.sms_per_hour": 30,
        # Dead port: the delivery notification must fail fast and be
        # swallowed (best effort), not fail the report.
        "agent.userbot_url": "http://127.0.0.1:1",
        "userbot_http.secret_env": "SIMBRIDGE_HTTP_SECRET",
    }
    app.state.ami = _FakeAMI()
    app.state.audit = FakeAudit()
    app.state.sms_limiter = RateLimiter(max_requests=1000,
                                        window_seconds=3600)
    app.state.blacklist = _NoBlacklist()
    app.state.sms_store = SMSCorrelationStore()
    app.state.metrics = metrics
    old_token, old_peers = deps._agent_token, deps._allowed_peers
    deps._agent_token = "s06-token"
    deps._allowed_peers = {"testclient"}
    yield TestClient(app), metrics, app.state
    deps._agent_token, deps._allowed_peers = old_token, old_peers


class TestSmsRouteMetrics:
    @staticmethod
    def _post(client, body):
        return client.post("/v1/sms", json=body,
                           headers={"Authorization": "Bearer s06-token"})

    def test_success_counts_sent(self, sms_env):
        client, metrics, _ = sms_env
        r = self._post(client,
                       {"to": "+79261234555", "text": "hi",
                        "telegram_user_id": 7})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert metrics.get_all()["sms"]["sent"] == 1
        assert metrics.get_all()["sms"]["failed"] == 0

    def test_ami_connection_error_counts_failed(self, sms_env):
        client, metrics, state = sms_env
        state.ami = _FakeAMI(exc=ConnectionError("ami down"))
        r = self._post(client,
                       {"to": "+79261234555", "text": "hi",
                        "telegram_user_id": 7})
        assert r.status_code == 503
        assert metrics.get_all()["sms"]["failed"] == 1
        assert metrics.get_all()["sms"]["sent"] == 0

    def test_blacklisted_not_counted(self, sms_env):
        client, metrics, state = sms_env
        state.blacklist = _AllBlacklist()
        r = self._post(client,
                       {"to": "+79261234555", "text": "hi",
                        "telegram_user_id": 7})
        assert r.status_code == 403
        assert metrics.get_all()["sms"]["failed"] == 0
        assert metrics.get_all()["sms"]["sent"] == 0

    def test_report_delivered_counts_delivered(self, sms_env):
        client, metrics, state = sms_env
        rec = state.sms_store.create(7, "+79261234555", "hello")
        state.sms_store.mark_submitted(rec.sms_id)
        r = client.post(
            "/v1/sms/report",
            json={"phone_number": "carrier",
                  "text": "Delivered 89261234555 2026-08-15 10:00",
                  "modem_id": "gsm"},
            headers={"Authorization": "Bearer s06-token"},
        )
        assert r.status_code == 200
        assert r.json()["matched"] is True
        assert r.json()["status"] == "delivered"
        assert metrics.get_all()["sms"]["delivered"] == 1

    def test_report_failed_counts_failed(self, sms_env):
        client, metrics, state = sms_env
        rec = state.sms_store.create(7, "+79261234555", "hello")
        state.sms_store.mark_submitted(rec.sms_id)
        r = client.post(
            "/v1/sms/report",
            json={"phone_number": "carrier",
                  "text": "Not delivered 89261234555 — expired",
                  "modem_id": "gsm"},
            headers={"Authorization": "Bearer s06-token"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "failed"
        assert metrics.get_all()["sms"]["failed"] == 1


# ---------------------------------------------------------------------------
# S06.2: modem poller — device state -> provider + metric
# ---------------------------------------------------------------------------

class _StatusAMI:
    def __init__(self, status=None, exc=None):
        self._status = status
        self._exc = exc

    async def get_modem_status(self):
        if self._exc is not None:
            raise self._exc
        return self._status


class TestPollerMetrics:
    def test_registered_feeds_provider_and_metric(self):
        provider = SingleModemProvider(modem_id="gsm")
        metrics = _RecMetrics()
        ami = _StatusAMI(status={"registered": True,
                                 "signal_percent": 80,
                                 "operator": "MTS"})
        ok = _run(poll_modem_state(ami, provider, "gsm", metrics=metrics))
        assert ok is True
        assert provider.get_info("gsm").state is ModemState.READY
        assert metrics.modem_registered is True

    def test_device_absent_offlines(self):
        provider = SingleModemProvider(modem_id="gsm")
        provider.update_state("gsm", registered=True, signal_percent=80)
        metrics = _RecMetrics()
        ami = _StatusAMI(status={})
        ok = _run(poll_modem_state(ami, provider, "gsm", metrics=metrics))
        assert ok is False
        assert provider.get_info("gsm").state is ModemState.OFFLINE
        assert metrics.modem_registered is False

    def test_ami_outage_keeps_state_and_skips_metric(self):
        provider = SingleModemProvider(modem_id="gsm")
        provider.update_state("gsm", registered=True, signal_percent=80)
        metrics = _RecMetrics()
        ami = _StatusAMI(exc=ConnectionError("ami down"))
        ok = _run(poll_modem_state(ami, provider, "gsm", metrics=metrics))
        assert ok is True  # last known state: READY -> available
        assert provider.get_info("gsm").state is ModemState.READY
        assert metrics.modem_registered == "unset"


# ---------------------------------------------------------------------------
# S06.2: userbot run_with_recovery — backoff reconnect on session drop
# ---------------------------------------------------------------------------

if "telethon" not in sys.modules:
    sys.modules["telethon"] = MagicMock(name="telethon")

import userbot.userbot as userbot_mod  # noqa: E402  (after the telethon stub)
from userbot.userbot import Userbot  # noqa: E402


class _StopLoop(Exception):
    """Raised by the fake client to end the run_with_recovery loop."""


class _FakeTGClient:
    def __init__(self, connect_failures=0):
        self._connect_calls = 0
        self._connect_failures = connect_failures
        self._disconnections = 0
        self.is_connected = True

    @property
    def connect_calls(self):
        return self._connect_calls

    async def run_until_disconnected(self):
        self._disconnections += 1
        if self._disconnections >= 2:
            raise _StopLoop()

    async def connect(self):
        self._connect_calls += 1
        if self._connect_calls <= self._connect_failures:
            raise ConnectionError("fake reconnect failure")
        return True


class _RecoveryAlerts:
    def __init__(self):
        self.calls = []

    async def alert(self, rule, message):
        self.calls.append((rule, message))
        return True


class TestRunWithRecovery:
    @staticmethod
    def _ub(client):
        # Bypass __init__ (needs cfg + a real TelegramClient); only
        # _client is touched by run_with_recovery.
        ub = object.__new__(Userbot)
        ub._client = client
        return ub

    def test_reconnects_and_continues(self, monkeypatch):
        monkeypatch.setattr(userbot_mod, "TG_RECONNECT_MIN_DELAY", 0.01)
        monkeypatch.setattr(userbot_mod, "TG_RECONNECT_MAX_DELAY", 0.01)
        client = _FakeTGClient()
        alerts = _RecoveryAlerts()
        with pytest.raises(_StopLoop):
            _run(self._ub(client).run_with_recovery(alerts=alerts))
        assert client.connect_calls == 1
        assert [c[0] for c in alerts.calls] == ["telegram_session_invalid"]

    def test_exhausted_retries_exit_cleanly(self, monkeypatch):
        monkeypatch.setattr(userbot_mod, "TG_RECONNECT_MIN_DELAY", 0.01)
        monkeypatch.setattr(userbot_mod, "TG_RECONNECT_MAX_DELAY", 0.01)
        monkeypatch.setattr(userbot_mod, "TG_RECONNECT_MAX_RETRIES", 2)
        client = _FakeTGClient(connect_failures=99)
        alerts = _RecoveryAlerts()
        # Must RETURN (systemd restarts the process), not raise.
        _run(self._ub(client).run_with_recovery(alerts=alerts))
        assert client.connect_calls == 2
        assert [c[0] for c in alerts.calls] == ["telegram_session_invalid"]
