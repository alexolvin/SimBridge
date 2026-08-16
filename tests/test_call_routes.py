"""Agent call-route tests (S04.3) — the dialplan/AGI-facing HTTP surface.

Covers the endpoints the AGI hooks and the userbot drive:

- /v1/call/incoming — register + TELEGRAM_RINGING (GSM leg NOT answered)
- /v1/call/outgoing — rate-limit -> ACL -> blacklist -> modem reservation
  -> TELEGRAM_CALLING (the out-of-band Telegram ring starts here)
- /v1/call/outgoing/accepted — the nocal gate: 404 when the call already
  expired (a late accept must not dial the GSM leg), 409 when the pending
  set is ambiguous
- /v1/call/{id}/complete — the DIALSTATUS outcome matrix, both directions
- /v1/call/check-timeouts — the ONLY enforcement of the out-of-band
  Telegram ring, plus the max_call_seconds reaper

The userbot is stood in for by a local capture HTTP server (same pattern
as test_agent_sms_report.py, reused here).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import deps
from agent.routes import router
from core.acl import ACLManager
from core.blacklist import BlacklistManager
from core.call_control import CallMachine, CallRegistry, CallState
from core.events import EventType
from core.ratelimit import RateLimiter
from tests.test_agent_sms_report import _CaptureServer, FakeAudit

ACL_USER = 123456789


def _audits(audit: FakeAudit, etype: EventType) -> list:
    """All audit kw-dicts logged for *etype* (audit.calls holds (type, kw))."""
    return [kw for (t, kw) in audit.calls if t == etype]


@pytest.fixture()
def capture():
    srv = _CaptureServer()
    yield srv
    srv.stop()


@pytest.fixture()
def env(tmp_path, capture, monkeypatch):
    """Real router + real registry/acl/blacklist/limiter; mocked AMI.

    Yields (client, registry, audit, ami, app, blacklist_path).
    """
    app = FastAPI()
    app.include_router(router, prefix="/v1")

    audit = FakeAudit()
    registry = CallRegistry(sms_store=MagicMock(), audit=audit)

    acl_path = tmp_path / "acl.conf"
    acl_path.write_text(f"{ACL_USER} out_call in_call\n")
    blacklist_path = tmp_path / "blacklist.txt"  # absent => empty blacklist

    ami = MagicMock()
    ami.hangup_channel = AsyncMock()

    app.state.cfg = {
        "agent.userbot_url": capture.url,
        "userbot_http.secret_env": "SIMBRIDGE_HTTP_SECRET",
        "asterisk": {"ring_wait_seconds": 24},
        "limits": {"max_call_seconds": 1800, "calls_per_minute": 3},
        "voice": {"outbound_answer_timeout": 30},
    }
    app.state.ami = ami
    app.state.audit = audit
    app.state.call_registry = registry
    app.state.acl = ACLManager(str(acl_path))
    app.state.blacklist = BlacklistManager(str(blacklist_path))
    app.state.call_limiter = RateLimiter(3, 60)

    monkeypatch.setenv("SIMBRIDGE_HTTP_SECRET", "sec")
    old_token, old_peers = deps._agent_token, deps._allowed_peers
    deps._agent_token = "test-token"
    # starlette TestClient connects from host "testclient"
    deps._allowed_peers = {"testclient"}

    yield TestClient(app), registry, audit, ami, app, blacklist_path

    deps._agent_token, deps._allowed_peers = old_token, old_peers


def _auth():
    return {"Authorization": "Bearer test-token"}


def _backdate(registry: CallRegistry, call_id: str, seconds: int) -> None:
    """Rewind updated_at so the call looks *seconds* old (windows are
    measured from the last state change)."""
    registry.get(call_id).updated_at = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat()


# =========================================================================
# /v1/call/incoming
# =========================================================================

class TestCallIncoming:
    def test_registers_and_starts_telegram_ring(self, env):
        client, registry, audit, ami, app, _ = env
        r = client.post(
            "/v1/call/incoming", headers=_auth(),
            json={"phone_number": "+79261234555",
                  "gsm_channel_id": "Dongle/gsm-0"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["direction"] == "incoming"
        # S04.3: the GSM leg is NOT answered yet — Telegram is ringing.
        assert body["state"] == CallState.TELEGRAM_RINGING.value
        assert registry.get(body["call_id"]).gsm_channel_id == "Dongle/gsm-0"
        assert _audits(audit, EventType.CALL_INCOMING)

    def test_missing_token_401(self, env):
        client, *_ = env
        r = client.post(
            "/v1/call/incoming",
            json={"phone_number": "+79261234555"},
        )
        assert r.status_code == 401

    def test_modem_id_from_configured_dongle(self, env):
        """S05.1 provenance: the node's configured dongle, not a default.
        cfg is wrapped in a DotDict as load_config() produces in production."""
        from core.config import DotDict

        client, registry, audit, ami, app, _ = env
        cfg = DotDict(app.state.cfg)
        cfg["asterisk"]["dongle"] = "ttyUSB0"
        app.state.cfg = cfg
        r = client.post(
            "/v1/call/incoming", headers=_auth(),
            json={"phone_number": "+79261234555",
                  "gsm_channel_id": "Dongle/ttyUSB0-0"},
        )
        assert r.status_code == 200
        assert registry.get(r.json()["call_id"]).modem_id == "ttyUSB0"


# =========================================================================
# /v1/call/outgoing
# =========================================================================

class TestCallOutgoing:
    def test_creates_call_and_starts_telegram_ring(self, env):
        client, registry, audit, ami, app, _ = env
        r = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["direction"] == "outgoing"
        assert body["state"] == CallState.TELEGRAM_CALLING.value
        assert body["caller_number"] == f"user:{ACL_USER}"
        assert body["callee_number"] == "+14155552671"
        assert _audits(audit, EventType.CALL_ACL_CHECK)[0]["outcome"] == "allowed"
        assert _audits(audit, EventType.CALL_OUTGOING)

    def test_acl_denied_before_session(self, env):
        """S04.3: no call session may exist after a denied ACL check."""
        client, registry, audit, ami, app, _ = env
        r = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": 999999999},
        )
        assert r.status_code == 403
        assert _audits(audit, EventType.CALL_ACL_CHECK)[0]["outcome"] == "denied"
        assert len(registry.list_all()) == 0

    def test_blacklisted_number_rejected(self, env):
        client, registry, audit, ami, app, blacklist_path = env
        # Hot-reload: the file appears after the manager was constructed.
        blacklist_path.write_text("+14155552671\n")
        r = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        )
        assert r.status_code == 403
        assert len(registry.list_all()) == 0

    def test_rate_limited_429(self, env):
        client, registry, audit, ami, app, _ = env
        app.state.call_limiter = RateLimiter(1, 60)
        r1 = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        )
        assert r1.status_code == 200
        # Free the modem so the second attempt reaches the limiter, not
        # the busy-modem check.
        call = registry.get(r1.json()["call_id"])
        registry.reject(call.call_id, reason="test")
        registry.cleanup(call.call_id)
        r2 = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552672",
                  "telegram_user_id": ACL_USER},
        )
        assert r2.status_code == 429

    def test_modem_busy_503(self, env):
        client, registry, audit, ami, app, _ = env
        r1 = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552672",
                  "telegram_user_id": ACL_USER},
        )
        assert r2.status_code == 503
        assert len(registry.list_all()) == 1  # no second session

    def test_offline_modem_503_message(self, env):
        """TS05-4: no reachable modem -> 503 with an 'offline' message,
        not the busy one (the operator action differs)."""
        from core.modem import ModemPool, SingleModemProvider

        client, registry, audit, ami, app, _ = env
        provider = SingleModemProvider(modem_id="gsm", device="gsm")  # OFFLINE
        pool = ModemPool(provider=provider)
        new_registry = CallRegistry(
            sms_store=MagicMock(), audit=audit, modem_pool=pool
        )
        app.state.call_registry = new_registry
        r = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        )
        assert r.status_code == 503
        assert "offline" in r.json()["detail"]
        assert len(new_registry.list_all()) == 0  # no session created


# =========================================================================
# /v1/call/outgoing/accepted — the nocal gate
# =========================================================================

class TestCallOutgoingAccepted:
    def test_no_pending_call_404(self, env):
        """A late accept after the TG ring expired must 404 — the AGI
        then leaves CALL_ID unset and the GSM dial is gated off (nocal)."""
        client, *_ = env
        r = client.post(
            "/v1/call/outgoing/accepted", headers=_auth(),
            json={"bridge_channel_id": ""},
        )
        assert r.status_code == 404

    def test_accept_dials_gsm(self, env):
        client, registry, audit, ami, app, _ = env
        call_id = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        ).json()["call_id"]
        r = client.post(
            "/v1/call/outgoing/accepted", headers=_auth(),
            json={"bridge_channel_id": "SIP/tg-bridge-00000002.0"},
        )
        assert r.status_code == 200
        assert r.json()["state"] == CallState.GSM_DIALING.value
        call = registry.get(call_id)
        assert call.bridge_channel_id == "SIP/tg-bridge-00000002.0"
        accepted = _audits(audit, EventType.CALL_ACCEPTED)
        assert accepted and accepted[0]["details"]["to"] == "+14155552671"

    def test_accept_from_modem_reserved(self, env):
        """The bridge may INVITE before start_telegram_calling was
        confirmed — the route catches the state up and dials."""
        client, registry, audit, ami, app, _ = env
        call = registry.create_outgoing(
            callee_number="+14155552671",
            caller_number=f"user:{ACL_USER}",
            telegram_user_id=ACL_USER,
        )
        assert call.state == CallState.MODEM_RESERVED
        r = client.post(
            "/v1/call/outgoing/accepted", headers=_auth(),
            json={"bridge_channel_id": ""},
        )
        assert r.status_code == 200
        assert registry.get(call.call_id).state == CallState.GSM_DIALING

    def test_ambiguous_pending_409(self, env):
        client, registry, audit, ami, app, _ = env
        client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        )
        # Inject a second pending call (whitebox: the single-modem
        # counter makes two pendings unreachable through the API).
        stray = CallMachine(call_id="stray", direction="outgoing",
                            callee_number="+14155552672")
        stray.transition(CallState.REQUESTED)
        stray.transition(CallState.ACL_CHECKED)
        stray.transition(CallState.MODEM_RESERVED)
        with registry._lock:
            registry._calls[stray.call_id] = stray
        r = client.post(
            "/v1/call/outgoing/accepted", headers=_auth(),
            json={"bridge_channel_id": ""},
        )
        assert r.status_code == 409


# =========================================================================
# /v1/call/{id}/complete — incoming (GSM -> Telegram)
# =========================================================================

class TestCallCompleteIncoming:
    def _register(self, client) -> str:
        return client.post(
            "/v1/call/incoming", headers=_auth(),
            json={"phone_number": "+79261234555",
                  "gsm_channel_id": "Dongle/gsm-0"},
        ).json()["call_id"]

    def test_answered_bridges_without_cleanup(self, env):
        """Dial returned ANSWERED: the call ran. Fast-forward to BRIDGED;
        no cleanup — the channel is alive until h-exten reports ENDED."""
        client, registry, audit, ami, app, _ = env
        call_id = self._register(client)
        r = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "answered"},
        )
        assert r.status_code == 200
        assert registry.get(call_id).state == CallState.BRIDGED
        assert _audits(audit, EventType.CALL_BRIDGED)
        assert call_id in [c.call_id for c in registry.list_active()]

    def test_no_answer_falls_back_to_voicemail(self, env):
        client, registry, audit, ami, app, _ = env
        call_id = self._register(client)
        r = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "no_answer", "dialstatus": "NOANSWER"},
        )
        assert r.status_code == 200
        tw = _audits(audit, EventType.CALL_TELEGRAM_TIMEOUT)
        assert tw and tw[0]["outcome"] == "voicemail_fallback"
        # Cleaned up (the dialplan takes the voicemail branch now).
        assert registry.get(call_id) is None
        assert registry.list_active() == []

    def test_busy_rejects(self, env):
        """TG user explicitly rejected (bridge 486/403)."""
        client, registry, audit, ami, app, _ = env
        call_id = self._register(client)
        r = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "busy"},
        )
        assert r.status_code == 200
        rej = _audits(audit, EventType.CALL_REJECTED)
        assert rej and rej[0]["outcome"] == "telegram_rejected"
        assert registry.get(call_id) is None

    def test_cancelled_hangs_up(self, env):
        """GSM caller hung up while Telegram was ringing."""
        client, registry, audit, ami, app, _ = env
        call_id = self._register(client)
        r = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "cancelled"},
        )
        assert r.status_code == 200
        hang = _audits(audit, EventType.CALL_HANGUP)
        assert hang and hang[0]["outcome"] == "cancelled"
        assert registry.get(call_id) is None

    def test_failed_hangs_up_with_dialstatus(self, env):
        """The bridge leg itself failed (e.g. bridge down)."""
        client, registry, audit, ami, app, _ = env
        call_id = self._register(client)
        r = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "failed", "dialstatus": "CHANUNAVAIL"},
        )
        assert r.status_code == 200
        hang = _audits(audit, EventType.CALL_HANGUP)
        assert hang and hang[0]["outcome"] == "failed"
        assert hang[0]["details"]["dialstatus"] == "CHANUNAVAIL"
        assert registry.get(call_id) is None

    def test_double_post_answered_409_then_ended_closes(self, env):
        """The dialplan reports the end in two events (s-exten ANSWERED,
        h-exten ENDED): the second ANSWERED is 409; ENDED hangs up the
        surviving leg via AMI and cleans up."""
        client, registry, audit, ami, app, _ = env
        call_id = self._register(client)
        r1 = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "answered"},
        )
        assert r1.status_code == 200
        r2 = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "answered"},
        )
        assert r2.status_code == 409
        r3 = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "ended", "dialstatus": "ENDED"},
        )
        assert r3.status_code == 200
        # Only the GSM leg was registered for this call.
        assert ami.hangup_channel.await_count == 1
        assert registry.get(call_id) is None

    def test_unknown_call_404(self, env):
        client, *_ = env
        r = client.post(
            "/v1/call/nonexistent/complete", headers=_auth(),
            json={"status": "ended"},
        )
        assert r.status_code == 404


# =========================================================================
# /v1/call/{id}/complete — outgoing (Telegram -> GSM)
# =========================================================================

class TestCallCompleteOutgoing:
    def _dial(self, client) -> str:
        """Outgoing call through to GSM_DIALING (user accepted, the
        bridge INVITEd, the dialplan dialed the Dongle)."""
        call_id = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        ).json()["call_id"]
        r = client.post(
            "/v1/call/outgoing/accepted", headers=_auth(),
            json={"bridge_channel_id": "SIP/tg-bridge-00000003.0"},
        )
        assert r.status_code == 200
        return call_id

    def test_answered_bridges_and_notifies(self, env, capture):
        client, registry, audit, ami, app, _ = env
        call_id = self._dial(client)
        r = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "answered"},
        )
        assert r.status_code == 200
        assert registry.get(call_id).state == CallState.BRIDGED
        assert _audits(audit, EventType.CALL_BRIDGED)
        # The userbot is notified (separate localized message).
        (req,) = capture.captured
        assert req["path"] == "/events/call"
        payload = json.loads(req["body"])
        assert payload["status"] == "answered"
        assert payload["to"] == "+14155552671"
        assert payload["telegram_user_id"] == ACL_USER
        assert payload["call_id"] == call_id
        headers = {k.lower(): v for k, v in req["headers"].items()}
        assert headers["x-simbridge-secret"] == "sec"
        # No cleanup — the SIP leg is alive until the TG user hangs up.
        assert call_id in [c.call_id for c in registry.list_active()]

    @pytest.mark.parametrize("status", ["no_answer", "busy", "failed"])
    def test_gsm_failure_notifies_and_cleans_up(self, env, capture, status):
        client, registry, audit, ami, app, _ = env
        call_id = self._dial(client)
        r = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": status},
        )
        assert r.status_code == 200
        hang = _audits(audit, EventType.CALL_HANGUP)
        assert hang and hang[0]["outcome"] == status
        (req,) = capture.captured
        assert json.loads(req["body"])["status"] == status
        # Terminal state -> cleaned up, modem released.
        assert registry.get(call_id) is None
        assert registry.list_active() == []
        assert registry.create_outgoing(
            callee_number="+14155552672",
            telegram_user_id=ACL_USER,
        ).state == CallState.MODEM_RESERVED

    def test_cancelled_no_notification(self, env, capture):
        """The TG user hung up while the GSM leg was dialing — no
        outcome message (they ended the call themselves)."""
        client, registry, audit, ami, app, _ = env
        call_id = self._dial(client)
        r = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "cancelled"},
        )
        assert r.status_code == 200
        assert _audits(audit, EventType.CALL_HANGUP)[0]["outcome"] == "cancelled"
        assert capture.captured == []
        assert registry.get(call_id) is None

    def test_bridged_ended_hangs_up_legs(self, env, capture):
        client, registry, audit, ami, app, _ = env
        call_id = self._dial(client)
        client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "answered"},
        )
        n_legs = len(registry.get(call_id).get_active_channel_ids())
        r = client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "ended", "dialstatus": "ENDED"},
        )
        assert r.status_code == 200
        assert ami.hangup_channel.await_count == n_legs
        assert registry.get(call_id) is None


# =========================================================================
# /v1/call/check-timeouts — the timeout driver
# =========================================================================

class TestCallCheckTimeouts:
    def test_no_overdue_calls_is_noop(self, env):
        client, registry, audit, ami, app, _ = env
        client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        )
        r = client.post("/v1/call/check-timeouts", headers=_auth())
        assert r.status_code == 200
        assert r.json() == {"timed_out": 0, "actions": []}

    def test_outgoing_ring_timeout_notifies_and_releases_modem(self, env, capture):
        """The out-of-band Telegram ring: THIS driver is the only
        enforcement. Expired -> TELEGRAM_TIMEOUT + user notified."""
        client, registry, audit, ami, app, _ = env
        call_id = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        ).json()["call_id"]
        _backdate(registry, call_id, 45)  # window is 30 s
        r = client.post("/v1/call/check-timeouts", headers=_auth())
        assert r.status_code == 200
        assert r.json() == {
            "timed_out": 1,
            "actions": [{"call_id": call_id, "action": "telegram_timeout"}],
        }
        assert _audits(audit, EventType.CALL_TELEGRAM_TIMEOUT)[0]["outcome"] == "no_answer"
        (req,) = capture.captured
        assert json.loads(req["body"])["status"] == "no_answer"
        assert registry.get(call_id) is None  # cleaned up
        # Modem released — a new call can reserve it.
        assert registry.create_outgoing(
            callee_number="+14155552672",
            telegram_user_id=ACL_USER,
        ).state == CallState.MODEM_RESERVED

    def test_incoming_ring_timeout_goes_to_voicemail(self, env):
        """Backstop for the dialplan's own Dial timeout (lost AGI event)."""
        client, registry, audit, ami, app, _ = env
        call_id = client.post(
            "/v1/call/incoming", headers=_auth(),
            json={"phone_number": "+79261234555"},
        ).json()["call_id"]
        _backdate(registry, call_id, 30)  # window is 24 s
        r = client.post("/v1/call/check-timeouts", headers=_auth())
        assert r.json() == {
            "timed_out": 1,
            "actions": [{"call_id": call_id, "action": "voicemail"}],
        }
        assert _audits(audit, EventType.CALL_TELEGRAM_TIMEOUT)[0]["outcome"] == "voicemail_fallback"
        assert registry.get(call_id) is None

    def test_bridged_max_duration_hangs_up_via_ami(self, env):
        client, registry, audit, ami, app, _ = env
        call_id = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        ).json()["call_id"]
        client.post(
            "/v1/call/outgoing/accepted", headers=_auth(),
            json={"bridge_channel_id": "SIP/tg-bridge-00000004.0"},
        )
        client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "answered"},
        )
        assert registry.get(call_id).state == CallState.BRIDGED
        _backdate(registry, call_id, 2000)  # window is 1800 s
        r = client.post("/v1/call/check-timeouts", headers=_auth())
        assert r.json() == {
            "timed_out": 1,
            "actions": [{"call_id": call_id, "action": "hangup_duration"}],
        }
        for _args, kwargs in ami.hangup_channel.await_args_list:
            assert kwargs.get("reason") == "max_duration"
        assert _audits(audit, EventType.CALL_DURATION_EXPIRED)
        assert registry.get(call_id) is None  # cleaned up

    def test_ami_failure_still_closes_call(self, env):
        """Documented behavior: the registry state transition does not
        depend on the AMI result — a dead Asterisk must not hold the
        modem reservation forever. The failed hangup is audited."""
        client, registry, audit, ami, app, _ = env
        call_id = client.post(
            "/v1/call/outgoing", headers=_auth(),
            json={"phone_number": "+14155552671",
                  "telegram_user_id": ACL_USER},
        ).json()["call_id"]
        client.post(
            "/v1/call/outgoing/accepted", headers=_auth(),
            json={"bridge_channel_id": "SIP/tg-bridge-00000005.0"},
        )
        client.post(
            f"/v1/call/{call_id}/complete", headers=_auth(),
            json={"status": "answered"},
        )
        _backdate(registry, call_id, 2000)
        ami.hangup_channel.side_effect = RuntimeError("AMI down")
        r = client.post("/v1/call/check-timeouts", headers=_auth())
        assert r.status_code == 200
        assert r.json()["actions"] == [
            {"call_id": call_id, "action": "hangup_duration"}
        ]
        partial = _audits(audit, EventType.CALL_HANGUP)
        assert any(kw["outcome"] == "partial_hangup" for kw in partial)
        assert registry.get(call_id) is None
        # Modem released despite the failed hangup.
        assert registry.create_outgoing(
            callee_number="+14155552672",
            telegram_user_id=ACL_USER,
        ).state == CallState.MODEM_RESERVED
