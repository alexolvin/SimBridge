"""Userbot HTTP server tests — Asterisk event delivery (D2/D4).

Drives ``create_http_server()`` with a fake Telethon client and asserts:
  - /events/sms routes "RING …" → in_call audience, real SMS → in_sms;
  - per-user delivery isolation (one failing recipient does not break
    the rest);
  - /events/delivery notifies ONLY the sender who sent the SMS (D4).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.acl import ACLManager
from core.audit import AuditLogger
from core.events import EventType
from userbot.http_server import create_http_server


class FakeClient:
    """Duck-typed Telethon client: records send_message/send_file calls."""

    def __init__(self, fail_for=frozenset()):
        self.sent = []    # list of (uid, text)
        self.files = []   # list of (uid, path, voice_note)
        self._fail_for = set(fail_for)

    async def send_message(self, uid, text):
        if uid in self._fail_for:
            raise RuntimeError(f"fake failure for {uid}")
        self.sent.append((uid, text))

    async def send_file(self, uid, path, voice_note=False):
        if uid in self._fail_for:
            raise RuntimeError(f"fake failure for {uid}")
        self.files.append((uid, path, voice_note))


class FakeAudit:
    """Records audit.log(event_type, **kw) calls."""

    def __init__(self):
        self.calls = []

    def log(self, event_type, **kw):
        self.calls.append((event_type, kw))


SECRET = "sec"


def _make_env(tmp_path, client=None, acl_lines="111 in_sms\n222 in_call\n"):
    acl_file = tmp_path / "acl.conf"
    acl_file.write_text(acl_lines)
    acl = ACLManager(str(acl_file))
    audit = FakeAudit()
    app = create_http_server(
        secret=SECRET,
        allowed_peers=[],
        acl=acl,
        audit=audit,
        client=client,
    )
    return TestClient(app), audit


# ---------------------------------------------------------------------------
# /events/sms — D2 audience routing
# ---------------------------------------------------------------------------

class TestEventsSms:
    def test_wrong_secret_401(self, tmp_path):
        client, _ = _make_env(tmp_path)
        r = client.post(
            "/events/sms",
            json={"phone_number": "+79261234555", "text": "hi"},
            headers={"X-SimBridge-Secret": "bad"},
        )
        assert r.status_code == 401

    def test_sms_goes_to_in_sms_audience(self, tmp_path):
        tg = FakeClient()
        client, audit = _make_env(
            tmp_path, client=tg,
            acl_lines="111 in_sms\n222 in_call\n333 out_sms\n",
        )
        r = client.post(
            "/events/sms",
            json={"phone_number": "+79261234555", "text": "Привет"},
            headers={"X-SimBridge-Secret": SECRET},
        )
        assert r.status_code == 200
        # Only the in_sms user gets the SMS — not the in_call / out_sms ones
        assert tg.sent == [(111, "SMS +79261234555:\nПривет")]
        (etype, kw), = audit.calls
        assert etype == EventType.SMS_RECEIVED
        assert kw["outcome"] == "ok"
        assert kw["details"]["kind"] == "sms"
        assert kw["details"]["delivered_to"] == [111]

    def test_ring_is_audited_but_not_sent(self, tmp_path):
        """RING: no Telegram text — the bridge's native incoming-call
        banner on the user's own account already signals the call, so
        the duplicate "Входящий звонок" line was removed at user
        request (2026-08-21). The event stays audited."""
        tg = FakeClient()
        client, audit = _make_env(
            tmp_path, client=tg,
            acl_lines="111 in_sms\n222 in_call\n",
        )
        r = client.post(
            "/events/sms",
            json={"phone_number": "+79261234555",
                  "text": "RING +79261234555"},
            headers={"X-SimBridge-Secret": SECRET},
        )
        assert r.status_code == 200
        body = r.json()
        assert tg.sent == []
        assert body["formatted_text"] is None
        assert body["delivered_to"] == []
        etype, kw = audit.calls[0]
        assert etype == EventType.SMS_RECEIVED
        assert kw["outcome"] == "suppressed"
        assert kw["details"]["kind"] == "ring"
        assert kw["details"]["audience"] == [222]

    def test_per_user_isolation_partial_failure(self, tmp_path):
        """One failing recipient must not break the rest (D2)."""
        tg = FakeClient(fail_for={222})
        client, audit = _make_env(
            tmp_path, client=tg,
            acl_lines="111 in_sms\n222 in_sms\n",
        )
        r = client.post(
            "/events/sms",
            json={"phone_number": "+79261234555", "text": "hi"},
            headers={"X-SimBridge-Secret": SECRET},
        )
        assert r.status_code == 200
        assert tg.sent == [(111, "SMS +79261234555:\nhi")]
        etype, kw = audit.calls[0]
        assert kw["outcome"] == "partial"
        assert kw["details"]["delivered_to"] == [111]

    def test_no_audience(self, tmp_path):
        tg = FakeClient()
        client, audit = _make_env(
            tmp_path, client=tg,
            acl_lines="111 out_sms\n",
        )
        r = client.post(
            "/events/sms",
            json={"phone_number": "+79261234555", "text": "hi"},
            headers={"X-SimBridge-Secret": SECRET},
        )
        assert r.status_code == 200
        assert tg.sent == []
        assert audit.calls[0][1]["outcome"] == "no_audience"

    def test_no_client_wired_not_delivered(self, tmp_path):
        client, audit = _make_env(tmp_path, client=None)
        r = client.post(
            "/events/sms",
            json={"phone_number": "+79261234555", "text": "hi"},
            headers={"X-SimBridge-Secret": SECRET},
        )
        assert r.status_code == 200  # accepted + audited
        assert r.json()["delivered_to"] == []


# ---------------------------------------------------------------------------
# /events/delivery — D4: notify ONLY the sender
# ---------------------------------------------------------------------------

class TestEventsDelivery:
    def _post(self, client, **body):
        return client.post(
            "/events/delivery", json=body,
            headers={"X-SimBridge-Secret": SECRET},
        )

    def test_delivered_notifies_only_sender(self, tmp_path):
        """The status goes to the SMS author (uid 7) — not to user 8."""
        tg = FakeClient()
        client, audit = _make_env(
            tmp_path, client=tg,
            acl_lines="7 in_sms\n8 in_sms\n",
        )
        r = self._post(
            client, sms_id="abc", phone_number="+79261234555",
            telegram_user_id=7, status="delivered",
        )
        assert r.status_code == 200
        assert r.json()["notified"] is True
        assert tg.sent == [(7, "Доставлено: +79261234555")]
        etype, kw = audit.calls[0]
        assert etype == EventType.SMS_DELIVERY_REPORT
        assert kw["outcome"] == "delivered"
        assert kw["telegram_user_id"] == 7
        assert kw["correlation_id"] == "abc"

    def test_failed_includes_error(self, tmp_path):
        tg = FakeClient()
        client, audit = _make_env(tmp_path, client=tg, acl_lines="7 in_sms\n")
        self._post(
            client, sms_id="abc", phone_number="+79261234555",
            telegram_user_id=7, status="failed", error="Expired",
        )
        (uid, text), = tg.sent
        assert uid == 7
        assert "SMS не доставлена" in text
        assert "+79261234555" in text
        assert "Expired" in text
        assert audit.calls[0][1]["outcome"] == "failed"

    def test_uid_zero_notifies_noone(self, tmp_path):
        """Sender unknown — audit only, no one to notify (D4)."""
        tg = FakeClient()
        client, audit = _make_env(tmp_path, client=tg, acl_lines="")
        r = self._post(
            client, sms_id="abc", phone_number="+79261234555",
            telegram_user_id=0, status="delivered",
        )
        assert r.json()["notified"] is False
        assert tg.sent == []

    def test_unknown_status_notified_false(self, tmp_path):
        tg = FakeClient()
        client, audit = _make_env(tmp_path, client=tg, acl_lines="7 in_sms\n")
        r = self._post(
            client, sms_id="abc", phone_number="+79261234555",
            telegram_user_id=7, status="weird",
        )
        assert r.status_code == 200
        assert r.json()["notified"] is False
        assert tg.sent == []
        assert audit.calls[0][1]["outcome"] == "unknown_status"

    def test_send_failure_swallowed(self, tmp_path):
        """A delivery-notify failure must not 500 the endpoint."""
        tg = FakeClient(fail_for={7})
        client, audit = _make_env(tmp_path, client=tg, acl_lines="7 in_sms\n")
        r = self._post(
            client, sms_id="abc", phone_number="+79261234555",
            telegram_user_id=7, status="delivered",
        )
        assert r.status_code == 200
        assert r.json()["notified"] is False


# ---------------------------------------------------------------------------
# /events/voicemail — S03.1/S03.3 delivery + cleanup
# ---------------------------------------------------------------------------

class TestEventsVoicemail:
    def _post(self, client, vm_type="normal", with_file=True,
              phone="+79261234555"):
        kwargs = {
            "data": {
                "phone_number": phone,
                "voicemail_type": vm_type,
                "correlation_id": "corr-vm-1",
                "duration": "12",
            },
            "headers": {"X-SimBridge-Secret": SECRET},
        }
        if with_file:
            kwargs["files"] = {"file": ("vm.opus", b"OggS-fake", "audio/ogg")}
        return client.post("/events/voicemail", **kwargs)

    def test_wrong_secret_401(self, tmp_path):
        client, _ = _make_env(tmp_path)
        r = client.post(
            "/events/voicemail",
            data={"phone_number": "+79261234555", "voicemail_type": "normal"},
            headers={"X-SimBridge-Secret": "bad"},
        )
        assert r.status_code == 401

    def test_normal_sends_label_and_voice_note(self, tmp_path):
        """S03.3: the in_call audience gets the label text plus the
        voice note; the uploaded audio does not survive the send."""
        tg = FakeClient()
        client, audit = _make_env(
            tmp_path, client=tg,
            acl_lines="111 in_sms\n222 in_call\n333 out_sms\n",
        )
        r = self._post(client, vm_type="normal")
        assert r.status_code == 200
        assert r.json()["delivered_to"] == [222]
        # only the in_call user — not in_sms / out_sms
        assert tg.sent == [(222, "🎙 Голосовое — +79261234555")]
        assert len(tg.files) == 1
        uid, path, voice_note = tg.files[0]
        assert uid == 222
        assert voice_note is True
        import os
        assert not os.path.isfile(path)  # S03.3: deleted after the send
        etype, kw = audit.calls[0]
        assert etype == EventType.VOICEMAIL_RECEIVED
        assert kw["outcome"] == "ok"
        assert kw["details"]["has_audio"] is True
        assert kw["details"]["delivered_to"] == [222]
        assert kw["correlation_id"] == "corr-vm-1"

    def test_early_hangup_is_text_only(self, tmp_path):
        """S03.1: even if audio arrived, early_hangup sends text only."""
        tg = FakeClient()
        client, audit = _make_env(tmp_path, client=tg)
        r = self._post(client, vm_type="early_hangup")
        assert r.status_code == 200
        assert tg.sent == [(222, "📞 Звонок — +79261234555")]
        assert tg.files == []  # no voice note for a greeting fragment
        etype, kw = audit.calls[0]
        assert etype == EventType.VOICEMAIL_EARLY_HANGUP
        assert kw["details"]["has_audio"] is False

    def test_urlencoded_early_hangup_event(self, tmp_path):
        """The sender's text-only shape (core.voicemail_forward
        post_form): urlencoded body, no file.

        Regression 2026-08-21: the sender used JSON, which
        req.form() parses as an EMPTY form — the event was
        misdelivered as "normal" from "unknown" with no audio."""
        tg = FakeClient()
        client, audit = _make_env(tmp_path, client=tg)
        r = client.post(
            "/events/voicemail",
            data={
                "phone_number": "+79261234555",
                "voicemail_type": "early_hangup",
                "correlation_id": "corr-vm-2",
                "duration": "8.24",
            },
            headers={"X-SimBridge-Secret": SECRET},
        )
        assert r.status_code == 200
        assert tg.sent == [(222, "📞 Звонок — +79261234555")]
        assert tg.files == []
        etype, kw = audit.calls[0]
        assert etype == EventType.VOICEMAIL_EARLY_HANGUP
        assert kw["correlation_id"] == "corr-vm-2"

    def test_json_body_rejected_415(self, tmp_path):
        """A JSON body would parse to an empty form and be silently
        misdelivered as "normal" — refuse it loudly instead so the
        sender keeps the recording for the next retry (2026-08-21)."""
        tg = FakeClient()
        client, _ = _make_env(tmp_path, client=tg)
        r = client.post(
            "/events/voicemail",
            json={"phone_number": "+79261234555",
                  "voicemail_type": "early_hangup"},
            headers={"X-SimBridge-Secret": SECRET},
        )
        assert r.status_code == 415
        assert tg.sent == []

    def test_recording_missing_is_text_only(self, tmp_path):
        tg = FakeClient()
        client, audit = _make_env(tmp_path, client=tg)
        r = self._post(client, vm_type="recording_missing", with_file=False)
        assert r.status_code == 200
        assert tg.sent == [(222, "⚠️ Нет записи — +79261234555")]
        assert tg.files == []

    def test_normal_without_file_is_text_only(self, tmp_path):
        tg = FakeClient()
        client, audit = _make_env(tmp_path, client=tg)
        r = self._post(client, vm_type="normal", with_file=False)
        assert r.status_code == 200
        assert tg.sent == [(222, "🎙 Голосовое — +79261234555")]
        assert tg.files == []
        assert audit.calls[0][1]["details"]["has_audio"] is False

    def test_no_client_wired_not_delivered(self, tmp_path):
        client, audit = _make_env(tmp_path, client=None)
        r = self._post(client, vm_type="normal")
        assert r.status_code == 200  # accepted + audited
        assert r.json()["delivered_to"] == []
        assert audit.calls[0][1]["outcome"] == "failed"

    def test_per_user_isolation_partial_failure(self, tmp_path):
        """One failing recipient must not break the rest (S03.3)."""
        tg = FakeClient(fail_for={222})
        client, audit = _make_env(
            tmp_path, client=tg,
            acl_lines="222 in_call\n333 in_call\n",
        )
        r = self._post(client, vm_type="normal")
        assert r.status_code == 200
        assert r.json()["delivered_to"] == [333]
        assert [uid for uid, _, _ in tg.files] == [333]
        assert audit.calls[0][1]["outcome"] == "partial"


# ---------------------------------------------------------------------------
# /events/call — S04.3 outgoing-call outcome notifications
# ---------------------------------------------------------------------------

class TestEventsCall:
    def _post(self, client, **body):
        return client.post(
            "/events/call", json=body,
            headers={"X-SimBridge-Secret": SECRET},
        )

    def test_wrong_secret_401(self, tmp_path):
        client, _ = _make_env(tmp_path)
        r = client.post(
            "/events/call",
            json={"to": "+14155552671", "telegram_user_id": 7,
                  "status": "answered", "call_id": "c1"},
            headers={"X-SimBridge-Secret": "bad"},
        )
        assert r.status_code == 401

    @pytest.mark.parametrize("status,snippet", [
        ("answered", "Соединено с "),
        ("no_answer", "Нет ответа: "),
        ("busy", "Занято: "),
        ("failed", "Ошибка сети: "),
    ])
    def test_outcome_notifies_only_caller(self, tmp_path, status, snippet):
        """S04.3: separate localized message per GSM outcome, going ONLY
        to the user who placed the call."""
        tg = FakeClient()
        client, audit = _make_env(
            tmp_path, client=tg,
            acl_lines="7 in_sms\n8 in_sms\n",
        )
        r = self._post(
            client, to="+14155552671", telegram_user_id=7,
            status=status, call_id="c1",
        )
        assert r.status_code == 200
        assert r.json()["notified"] is True
        assert tg.sent == [(7, f"{snippet}+14155552671")]
        etype, kw = audit.calls[0]
        assert etype == EventType.CALL_HANGUP
        assert kw["outcome"] == status
        assert kw["telegram_user_id"] == 7
        assert kw["correlation_id"] == "c1"
        assert kw["details"] == {"to": "+14155552671", "notified": True}

    def test_unknown_status_notified_false(self, tmp_path):
        tg = FakeClient()
        client, audit = _make_env(tmp_path, client=tg, acl_lines="7 in_sms\n")
        r = self._post(
            client, to="+14155552671", telegram_user_id=7,
            status="weird", call_id="c1",
        )
        assert r.status_code == 200
        assert r.json()["notified"] is False
        assert tg.sent == []
        assert audit.calls[0][1]["outcome"] == "unknown_status"

    def test_uid_zero_notifies_noone(self, tmp_path):
        """Caller unknown (uid 0) — audit only (S04.3)."""
        tg = FakeClient()
        client, audit = _make_env(tmp_path, client=tg, acl_lines="")
        r = self._post(
            client, to="+14155552671", telegram_user_id=0,
            status="answered", call_id="c1",
        )
        assert r.json()["notified"] is False
        assert tg.sent == []
        assert audit.calls[0][1]["outcome"] == "answered"

    def test_no_client_wired_not_notified(self, tmp_path):
        client, audit = _make_env(tmp_path, client=None)
        r = self._post(
            client, to="+14155552671", telegram_user_id=7,
            status="answered", call_id="c1",
        )
        assert r.status_code == 200
        assert r.json()["notified"] is False
        assert audit.calls[0][1]["details"]["notified"] is False

    def test_send_failure_swallowed(self, tmp_path):
        """A notification failure must not 500 the endpoint (best-effort
        by contract — the agent already logged the outcome)."""
        tg = FakeClient(fail_for={7})
        client, audit = _make_env(tmp_path, client=tg, acl_lines="7 in_sms\n")
        r = self._post(
            client, to="+14155552671", telegram_user_id=7,
            status="no_answer", call_id="c1",
        )
        assert r.status_code == 200
        assert r.json()["notified"] is False


# ---------------------------------------------------------------------------
# Number-attempt dispatch (msg #48, 2026-08-22) — the userbot-side
# entry point for outgoing calls
# ---------------------------------------------------------------------------

# userbot.userbot imports telethon at module level; the function under
# test (is_number_attempt) is pure and does not need it. A stub module
# satisfies the import without installing telethon.
import sys  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

if "telethon" not in sys.modules:
    sys.modules["telethon"] = MagicMock(name="telethon")

from userbot.userbot import is_number_attempt  # noqa: E402


class TestIsNumberAttempt:
    """Dispatch rule (msg #48): a message whose stripped form starts
    with a digit or '+' is a number ATTEMPT — the strict validator
    (core.phone.parse_destination) then decides valid vs error. This
    function only answers "did the user try to type a number?". Text
    starting with a letter is never an attempt.

    Replaces TestExtractCallRequest: the old bare-number regex
    SILENCED short numbers (a PIN, an extension) and DIALED 8-10 digit
    strings as foreign numbers; now every number-looking message gets
    the validator's verdict.
    """

    @pytest.mark.parametrize("text", [
        "+79261234555",
        "89261234555",
        "79261234555",           # 7-prefix: attempt, rejected by the validator
        "+14155552671",
        "  +79261234555  ",      # surrounding whitespace is stripped
        "+7 (926) 123-45-55",    # formatted: attempt, rejected
        "+7-926-123-45-55",
        "8989123456",            # 10-digit 8-prefix: attempt, rejected
        "123",                   # 3 digits: attempt, VALID service number
        "1234",
        "12345678",              # PIN-like: attempt, rejected (no false-positive call)
        "1234567",               # 7 digits: attempt, rejected
        "+1234567",
        "123456789 extra",       # text after the number: attempt, rejected
        "+79261234555: hi",      # other characters: attempt, rejected
        "+79261234555@telegram",
        "12.345.678",
        "+",                     # bare plus: attempt, rejected
    ])
    def test_number_attempt(self, text):
        assert is_number_attempt(text) is True

    @pytest.mark.parametrize("text", [
        None,
        "",
        "   ",
        "привет",
        "Привет 2026",           # starts with a letter — plain text
        "тел: +79261234555",
    ])
    def test_not_a_number_attempt(self, text):
        assert is_number_attempt(text) is False
