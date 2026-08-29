"""Unit tests for userbot command handling (2026-08-22 UX batch).

Covers the user-requested behaviors:
  A. case-insensitive command keys — "/SMS ..." == "/sms ..." (msg #47);
  B. error replies for unknown commands ("/EEE ...") and malformed
     numbers ("неправильный номер для СМС" / "для звонка") — and only
     AFTER the access gate: unknown user -> total silence, known user
     without the right -> "Недостаточно прав" (msg #47);
  C. a plain reply (no /sms prefix) to a forwarded incoming SMS is
     sent to that number (msg #47);
  D. strict destination whitelist (msg #48): a call destination is
     accepted only as '+'+11-15 digits, '8'+11-15 digits total, a
     3-digit local service number, or a 4-digit internal network
     number; SMS accepts the two carrier forms only (service numbers
     and internal extensions cannot receive SMS); anything else gets
     an error reply instead of being silently normalized or dialed.

telethon is not installed in the test environment, so it is stubbed
with a MagicMock (same pattern as test_s06_wiring.py) and the handler
closures are captured from a fake client and invoked directly with a
fake event — no real Telegram dispatch, no real HTTP (httpx.AsyncClient
is patched where a submission would happen).
"""

from __future__ import annotations

import asyncio
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

if "telethon" not in sys.modules:
    sys.modules["telethon"] = MagicMock(name="telethon")

import userbot.userbot as userbot_mod  # noqa: E402  (after the telethon stub)
from userbot.userbot import (  # noqa: E402
    Userbot,
    command_token,
    is_plain_reply_candidate,
    is_unknown_command,
)
from core.errors import SMSErrorType  # noqa: E402
from core.events import EventType  # noqa: E402


def _run(coro):
    """Run a coroutine on a fresh loop, then close it (see test_s06_wiring)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

MASTER = 449550030
PARTIAL = 800682480  # known, but only "in_call" — no out_sms/out_call
STRANGER = 111111111  # not in the ACL at all


class FakeClient:
    """Records (spec, fn) pairs from client.on(); send_message recorder."""

    def __init__(self):
        self.handlers = {}
        self.sent = []

    def on(self, spec):
        def decorator(fn):
            self.handlers[fn.__name__] = (spec, fn)
            return fn

        return decorator

    def spec(self, name):
        return self.handlers[name][0]

    def fn(self, name):
        return self.handlers[name][1]

    async def send_message(self, uid, text):
        self.sent.append((uid, text))


class RecordingEvents:
    """Stand-in for telethon.events: NewMessage(...) records its spec."""

    def NewMessage(self, pattern=None, func=None):
        return {"pattern": pattern, "func": func}


class FakeAcl:
    def __init__(self, users):
        self.users = users  # uid -> set(rights)

    def get_user_rights(self, uid):
        return self.users.get(uid, set())

    def check(self, uid, right):
        return right in self.users.get(uid, set())

    def users_with_right(self, right):
        return [u for u, r in sorted(self.users.items()) if right in r]


class FakeAudit:
    def __init__(self):
        self.calls = []

    def log(self, event_type, **kw):
        self.calls.append((event_type, kw))


class FakeBlacklist:
    def __init__(self, nums=()):
        self.nums = set(nums)

    def contains(self, n):
        return n in self.nums

    def block(self, n):
        self.nums.add(n)

    def unblock(self, n):
        self.nums.discard(n)


class FakeMsg:
    def __init__(self, text, mid=101):
        self.text = text
        self.id = mid


class FakeEvt:
    def __init__(self, text, sender_id, reply_to=None, voice=False, mid=101):
        self.message = FakeMsg(text, mid)
        self.sender_id = sender_id
        self.text = text
        self.id = mid
        self.voice = voice
        self.is_reply = reply_to is not None
        self.reply_to_msg_id = reply_to.id if reply_to else None
        self.reply_to = reply_to
        self.replies = []

    async def reply(self, text, *a, **k):
        self.replies.append(text)

    async def get_reply_message(self):
        return self.reply_to


def make_ub(monkeypatch, rights=None, blacklist=()):
    """Build a Userbot with fakes (bypassing __init__) and register
    its handlers on the fake client."""
    monkeypatch.setattr(userbot_mod, "events", RecordingEvents())
    client = FakeClient()
    acl = FakeAcl(
        rights
        if rights is not None
        else {MASTER: {"out_sms", "out_call", "in_sms", "in_call"},
              PARTIAL: {"in_call"}}
    )
    ub = object.__new__(Userbot)
    ub._client = client
    ub._acl = acl
    ub._audit = FakeAudit()
    ub._blacklist = FakeBlacklist(blacklist)
    ub._agent_url = "http://127.0.0.1:8090"
    ub._agent_token = "test-token"
    ub._bridge = MagicMock()
    ub._register_handlers()
    return ub, client


class FakeResp:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://127.0.0.1:8090/v1/sms"),
                response=self,
            )

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def fake_http(monkeypatch, resp=None, exc=None):
    """Patch httpx.AsyncClient (imported inside the handler); returns the
    list of recorded POSTs."""
    posts = []

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            posts.append({"url": url, "json": json, "headers": headers})
            if exc is not None:
                raise exc
            return resp if resp is not None else FakeResp(200, {})

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return posts


# ---------------------------------------------------------------------------
# Pure classification helpers
# ---------------------------------------------------------------------------

class TestCommandToken:
    def test_lowercases_first_token(self):
        assert command_token("/SMS +7989 hi") == "sms"
        assert command_token("/Eee x") == "eee"
        assert command_token("/help") == "help"

    def test_no_slash_is_not_a_command(self):
        assert command_token("hello") is None
        assert command_token("+79991234567") is None
        assert command_token("") is None
        assert command_token(None) is None

    def test_slash_without_token(self):
        assert command_token("/") is None
        assert command_token("/   ") is None

    def test_leading_whitespace(self):
        assert command_token("  /SMS x") == "sms"


class TestClassifiers:
    def test_unknown_command(self):
        assert is_unknown_command("/EEE ...") is True
        assert is_unknown_command("/Sms 989..") is False  # known token
        assert is_unknown_command("/help") is False
        assert is_unknown_command("plain") is False
        assert is_unknown_command(None) is False

    def test_plain_reply_candidate(self):
        assert is_plain_reply_candidate("Ответ") is True
        assert is_plain_reply_candidate("/sms +7.. hi") is False
        assert is_plain_reply_candidate("+79991234567") is False


# ---------------------------------------------------------------------------
# A. case-insensitive command keys
# ---------------------------------------------------------------------------

class TestCaseInsensitivePatterns:
    def test_patterns_match_uppercase_and_mixed(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        cases = {
            "handle_sms": ["/SMS +79991234567 hi", "/Sms +79991234567 hi"],
            "handle_broadcast": ["/Broadcast hi", "/bRoAdCaSt hi"],
            "handle_block": ["/BLOCK +79991234567", "/Block +79991234567"],
            "handle_unblock": ["/UNBLOCK +79991234567", "/Unblock +79991234567"],
            "handle_help": ["/HELP", "/Help"],
        }
        for name, samples in cases.items():
            rx = re.compile(client.spec(name)["pattern"])
            for s in samples:
                assert rx.match(s), f"{name}: pattern {rx.pattern!r} !~ {s!r}"

    def test_patterns_still_match_lowercase(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        cases = {
            "handle_sms": "/sms +79991234567 hi",
            "handle_broadcast": "/broadcast hi",
            "handle_block": "/block +79991234567",
            "handle_unblock": "/unblock +79991234567",
            "handle_help": "/help",
        }
        for name, s in cases.items():
            rx = re.compile(client.spec(name)["pattern"])
            assert rx.match(s), f"{name}: pattern {rx.pattern!r} !~ {s!r}"

    def test_word_boundary_still_enforced(self, monkeypatch):
        # /smsx must NOT trigger the /sms handler (\b after the token)
        ub, client = make_ub(monkeypatch)
        rx = re.compile(client.spec("handle_sms")["pattern"])
        assert rx.match("/smsx +79991234567 hi") is None


# ---------------------------------------------------------------------------
# B. access gate — unknown: silence; known without right: DENIED
# ---------------------------------------------------------------------------

class TestAccessGate:
    def test_unknown_user_silence_and_audit(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt("/sms +79991234567 hi", STRANGER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == []
        et, kw = ub._audit.calls[-1]
        assert et == EventType.USER_DENIED
        assert kw["outcome"] == "unknown_user"

    def test_partial_access_gets_denied_not_silence(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        # PARTIAL has in_call only — /sms needs out_sms
        evt = FakeEvt("/sms +79991234567 hi", PARTIAL)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == [SMSErrorType.DENIED.value]

    def test_gate_covers_every_command(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        # (handler, message for a known user WITHOUT the needed right)
        denied = {
            "handle_sms": "/sms +79991234567 hi",
            "handle_broadcast": "/broadcast hi",
            "handle_block": "/block +79991234567",
            "handle_unblock": "/unblock +79991234567",
        }
        for name, text in denied.items():
            evt = FakeEvt(text, PARTIAL)
            _run(client.fn(name)(evt))
            assert evt.replies == [SMSErrorType.DENIED.value], name
        # unknown-command / help / bare-number are known-user notices,
        # not rights-gated — PARTIAL still gets a reply
        evt = FakeEvt("/EEE", PARTIAL)
        _run(client.fn("handle_unknown_command")(evt))
        assert evt.replies and "Неизвестная команда" in evt.replies[0]
        evt = FakeEvt("/help", PARTIAL)
        _run(client.fn("handle_help")(evt))
        assert evt.replies  # at least the header line

    def test_stranger_silence_on_every_command(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        messages = {
            "handle_sms": "/sms +79991234567 hi",
            "handle_broadcast": "/broadcast hi",
            "handle_block": "/block +79991234567",
            "handle_unblock": "/unblock +79991234567",
            "handle_help": "/help",
            "handle_unknown_command": "/EEE whatever",
            "handle_bare_number": "+79991234567",
            "handle_sms_reply": "Ответ",
            "handle_voice_note": None,
        }
        for name, text in messages.items():
            reply_to = FakeMsg("SMS +79991234567: Вход") if name == "handle_sms_reply" else None
            voice = name == "handle_voice_note"
            evt = FakeEvt(text, STRANGER, reply_to=reply_to, voice=voice)
            _run(client.fn(name)(evt))
            assert evt.replies == [], f"{name}: stranger must get no reply"
        assert posts == []  # nothing submitted for a stranger
        assert client.sent == []  # no broadcast for a stranger


# ---------------------------------------------------------------------------
# B. unknown command
# ---------------------------------------------------------------------------

class TestUnknownCommand:
    def test_known_user_gets_reply(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt("/EEE whatever", MASTER)
        _run(client.fn("handle_unknown_command")(evt))
        assert evt.replies == [
            f"{SMSErrorType.UNKNOWN_COMMAND.value}. /help — список команд."
        ]

    def test_stranger_silence_and_audit(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt("/EEE", STRANGER)
        _run(client.fn("handle_unknown_command")(evt))
        assert evt.replies == []
        et, kw = ub._audit.calls[-1]
        assert et == EventType.USER_DENIED
        assert kw["outcome"] == "unknown_user"
        assert kw["details"]["reason"] == "unknown_command"

    def test_filter_excludes_known_commands_and_plain_text(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        f = client.spec("handle_unknown_command")["func"]
        assert f(FakeEvt("/EEE", MASTER)) is True
        assert f(FakeEvt("/Sms 989..", MASTER)) is False
        assert f(FakeEvt("/help", MASTER)) is False
        assert f(FakeEvt("plain text", MASTER)) is False
        assert f(FakeEvt(None, MASTER)) is False


# ---------------------------------------------------------------------------
# B. malformed numbers — context-specific messages, nothing submitted
# ---------------------------------------------------------------------------

class TestMalformedNumbers:
    def test_sms_short_number(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/sms 989 hello", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == [SMSErrorType.NUMBER_MALFORMED_SMS.value]
        assert posts == []

    def test_sms_letters(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/sms abc hello", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == [SMSErrorType.NUMBER_MALFORMED_SMS.value]
        assert posts == []

    def test_bare_number_too_long(self, monkeypatch):
        # 17 digits with '+': a number attempt (reaches the handler),
        # but outside the msg #48 whitelist (11-15 digits excl. '+')
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("+79991234567123456", MASTER)
        _run(client.fn("handle_bare_number")(evt))
        assert evt.replies == [SMSErrorType.NUMBER_MALFORMED_CALL.value]
        assert posts == []

    def test_block_short_number_uses_generic_message(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/block 989", MASTER)
        _run(client.fn("handle_block")(evt))
        assert evt.replies == [SMSErrorType.NUMBER_MALFORMED.value]
        assert posts == []


# ---------------------------------------------------------------------------
# /sms submission (shared path _do_send_sms)
# ---------------------------------------------------------------------------

class TestSmsSend:
    def test_success_uppercase_command(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/SMS +79991234567 Выход", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == ["Отправлено"]
        assert len(posts) == 1
        p = posts[0]
        assert p["url"] == "http://127.0.0.1:8090/v1/sms"
        assert p["json"]["to"] == "+79991234567"
        assert p["json"]["text"] == "Выход"
        assert p["headers"]["Authorization"] == "Bearer test-token"
        assert p["headers"]["x-correlation-id"] == p["json"]["correlation_id"]

    def test_sms_8_prefix_normalized_to_plus7(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/sms 89991234567 hi", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == ["Отправлено"]
        assert posts[0]["json"]["to"] == "+79991234567"

    def test_sms_7_prefix_rejected(self, monkeypatch):
        # the 7-prefix was silently normalized before msg #48
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/sms 79991234567 hi", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == [SMSErrorType.NUMBER_MALFORMED_SMS.value]
        assert posts == []

    @pytest.mark.parametrize("num", ["1234", "100"])
    def test_sms_to_non_carrier_rejected(self, monkeypatch, num):
        # a 4-digit number is an internal network extension and a
        # 3-digit one a local service number: both can be dialed, but
        # neither can receive SMS
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt(f"/sms {num} hi", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == [SMSErrorType.NUMBER_MALFORMED_SMS.value]
        assert posts == []

    def test_agent_403_maps_to_blacklisted(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        fake_http(monkeypatch, FakeResp(403, {"detail": "blacklisted"}))
        evt = FakeEvt("/sms +79991234567 x", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == [SMSErrorType.BLACKLISTED.value]

    def test_agent_429_maps_to_rate_limit(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        fake_http(monkeypatch, FakeResp(429, {"detail": "slow down"}))
        evt = FakeEvt("/sms +79991234567 x", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == ["Слишком много SMS. Попробуйте позже."]

    def test_agent_5xx_detail_surfaced(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        fake_http(monkeypatch, FakeResp(502, {"detail": "Asterisk AMI unavailable"}))
        evt = FakeEvt("/sms +79991234567 x", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == ["Ошибка отправки: Asterisk AMI unavailable"]

    def test_connect_error_maps_to_modem_unavailable(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        fake_http(monkeypatch, exc=httpx.ConnectError("boom"))
        evt = FakeEvt("/sms +79991234567 x", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == [SMSErrorType.MODEM_UNAVAILABLE.value]

    def test_local_blacklist_blocks_before_submission(self, monkeypatch):
        ub, client = make_ub(monkeypatch, blacklist=("+79991234567",))
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/sms +79991234567 x", MASTER)
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == [SMSErrorType.BLACKLISTED.value]
        assert posts == []


# ---------------------------------------------------------------------------
# C. plain reply to a forwarded incoming SMS
# ---------------------------------------------------------------------------

class TestSmsReply:
    @staticmethod
    def sms_msg(num="+79991234567", text="Вход"):
        return FakeMsg(f"SMS {num}: {text}")

    def test_plain_reply_sends_to_that_number(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("Ответ", MASTER, reply_to=self.sms_msg())
        _run(client.fn("handle_sms_reply")(evt))
        assert evt.replies == ["Отправлено"]
        assert posts[0]["json"]["to"] == "+79991234567"
        assert posts[0]["json"]["text"] == "Ответ"

    def test_sms_prefixed_reply_case_insensitive_prefix_strip(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/SMS Выход", MASTER, reply_to=self.sms_msg())
        _run(client.fn("handle_sms")(evt))
        assert evt.replies == ["Отправлено"]
        assert posts[0]["json"]["to"] == "+79991234567"
        assert posts[0]["json"]["text"] == "Выход"

    def test_reply_to_non_sms_message_is_silence(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("Ответ", MASTER, reply_to=FakeMsg("hello there"))
        _run(client.fn("handle_sms_reply")(evt))
        assert evt.replies == []
        assert posts == []

    def test_stranger_reply_silence(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt("Ответ", STRANGER, reply_to=self.sms_msg())
        _run(client.fn("handle_sms_reply")(evt))
        assert evt.replies == []

    def test_partial_access_reply_denied(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt("Ответ", PARTIAL, reply_to=self.sms_msg())
        _run(client.fn("handle_sms_reply")(evt))
        assert evt.replies == [SMSErrorType.DENIED.value]

    def test_filter(self, monkeypatch):
        # Telethon dispatches on truthiness, so negatives assert falsy
        # (an `and` chain may return the first falsy value, not False).
        ub, client = make_ub(monkeypatch)
        f = client.spec("handle_sms_reply")["func"]
        assert f(FakeEvt("Ответ", MASTER, reply_to=self.sms_msg())) is True
        assert not f(FakeEvt("/sms +7.. x", MASTER, reply_to=self.sms_msg()))
        assert not f(FakeEvt("+79991234567", MASTER, reply_to=self.sms_msg()))
        assert not f(FakeEvt("Ответ", MASTER))  # not a reply
        assert not f(FakeEvt("", MASTER, reply_to=self.sms_msg()))  # empty
        assert not f(FakeEvt("Ответ", MASTER, reply_to=self.sms_msg(),
                             voice=True))


# ---------------------------------------------------------------------------
# /help, /block, /unblock, /broadcast
# ---------------------------------------------------------------------------

class TestHelp:
    def test_known_user_gets_help(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt("/HELP", MASTER)
        _run(client.fn("handle_help")(evt))
        assert len(evt.replies) == 1
        assert "SimBridge commands:" in evt.replies[0]
        assert "/sms" in evt.replies[0]

    def test_stranger_silence(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt("/help", STRANGER)
        _run(client.fn("handle_help")(evt))
        assert evt.replies == []


class TestBlockUnblock:
    def test_block_case_insensitive_success(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/BLOCK +79991234567", MASTER)
        _run(client.fn("handle_block")(evt))
        assert evt.replies == ["Number +79991234567 blocked."]
        assert ub._blacklist.contains("+79991234567")
        assert posts[0]["url"] == "http://127.0.0.1:8090/v1/blacklist"

    def test_stranger_block_is_silence_and_no_effect(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/block +79991234567", STRANGER)
        _run(client.fn("handle_block")(evt))
        assert evt.replies == []
        assert posts == []
        assert not ub._blacklist.contains("+79991234567")

    def test_unblock(self, monkeypatch):
        ub, client = make_ub(monkeypatch, blacklist=("+79991234567",))
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/UNBLOCK +79991234567", MASTER)
        _run(client.fn("handle_unblock")(evt))
        assert evt.replies == ["Number +79991234567 unblocked."]
        assert not ub._blacklist.contains("+79991234567")
        assert posts[0]["url"] == "http://127.0.0.1:8090/v1/unblock"

    @pytest.mark.parametrize("num", ["1234", "100"])
    def test_block_non_carrier_rejected(self, monkeypatch, num):
        # a 4-digit internal extension and a 3-digit service number are
        # not carrier numbers — they cannot be blocked
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt(f"/block {num}", MASTER)
        _run(client.fn("handle_block")(evt))
        assert evt.replies == [SMSErrorType.NUMBER_MALFORMED.value]
        assert posts == []
        assert not ub._blacklist.contains(num)

    def test_unblock_7_prefix_rejected(self, monkeypatch):
        ub, client = make_ub(monkeypatch, blacklist=("+79991234567",))
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("/unblock 79991234567", MASTER)
        _run(client.fn("handle_unblock")(evt))
        assert evt.replies == [SMSErrorType.NUMBER_MALFORMED.value]
        assert posts == []


class TestBroadcast:
    def test_case_insensitive_success(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt("/Broadcast hi", MASTER)
        _run(client.fn("handle_broadcast")(evt))
        # only MASTER has out_sms in the default fixture
        assert client.sent == [(MASTER, "hi")]
        assert evt.replies == ["Рассылка: доставлено 1 из 1"]

    def test_stranger_broadcast_silence_and_no_send(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt("/broadcast hi", STRANGER)
        _run(client.fn("handle_broadcast")(evt))
        assert evt.replies == []
        assert client.sent == []


class TestBareNumber:
    def test_stranger_silence_and_audit(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt("+79991234567", STRANGER)
        _run(client.fn("handle_bare_number")(evt))
        assert evt.replies == []
        et, kw = ub._audit.calls[-1]
        assert et == EventType.USER_DENIED
        assert kw["outcome"] == "unknown_user"

    def test_partial_access_denied(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {}))
        evt = FakeEvt("+79991234567", PARTIAL)
        _run(client.fn("handle_bare_number")(evt))
        assert evt.replies == [SMSErrorType.DENIED.value]
        assert posts == []

    def test_voice_note_stranger_silence(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        evt = FakeEvt(None, STRANGER, voice=True)
        _run(client.fn("handle_voice_note")(evt))
        assert evt.replies == []

    # ---- msg #48: strict whitelist for call destinations ----

    def test_plus_form_dials(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        ub._bridge.start_call = AsyncMock(return_value=True)
        posts = fake_http(monkeypatch, FakeResp(200, {"call_id": "c1"}))
        evt = FakeEvt("+79991234567", MASTER)
        _run(client.fn("handle_bare_number")(evt))
        assert posts[0]["json"]["phone_number"] == "+79991234567"
        assert evt.replies == ["Звоню вам в Telegram…"]

    def test_8_prefix_normalized_then_dialed(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        ub._bridge.start_call = AsyncMock(return_value=True)
        posts = fake_http(monkeypatch, FakeResp(200, {"call_id": "c1"}))
        evt = FakeEvt("89991234567", MASTER)
        _run(client.fn("handle_bare_number")(evt))
        assert posts[0]["json"]["phone_number"] == "+79991234567"
        assert evt.replies == ["Звоню вам в Telegram…"]

    def test_service_3_digit_dials_as_is(self, monkeypatch):
        # a 3-digit number is a local service number (e.g. 100, Moscow
        # time service) — external, dialed as-is via the GSM modem
        ub, client = make_ub(monkeypatch)
        ub._bridge.start_call = AsyncMock(return_value=True)
        posts = fake_http(monkeypatch, FakeResp(200, {"call_id": "c1"}))
        evt = FakeEvt("123", MASTER)
        _run(client.fn("handle_bare_number")(evt))
        assert posts[0]["json"]["phone_number"] == "123"
        assert evt.replies == ["Звоню вам в Telegram…"]

    def test_internal_4_digit_dials_as_is(self, monkeypatch):
        ub, client = make_ub(monkeypatch)
        ub._bridge.start_call = AsyncMock(return_value=True)
        posts = fake_http(monkeypatch, FakeResp(200, {"call_id": "c1"}))
        evt = FakeEvt("1234", MASTER)
        _run(client.fn("handle_bare_number")(evt))
        assert posts[0]["json"]["phone_number"] == "1234"
        assert evt.replies == ["Звоню вам в Telegram…"]

    @pytest.mark.parametrize(
        "text",
        [
            "9881234567",          # 10 digits — was silently dialed before
            "8989123456",          # 8-prefix but only 10 digits total
            "79991234567",         # 7-prefix not on the whitelist
            "12",                  # 2 digits — no rule matches
            "12345",               # 5 digits — no rule matches
            "12345678",            # PIN-like 8 digits — was a false-positive call
            "+7 (926) 123-45-55",  # formatted — must not be normalized
            "+7-926-123-45-55",
            "+1234567",            # only 7 digits after '+'
            "89a12345678",         # letters
            "123456789 extra",     # text after the number
        ],
    )
    def test_malformed_number_error_no_post(self, monkeypatch, text):
        ub, client = make_ub(monkeypatch)
        posts = fake_http(monkeypatch, FakeResp(200, {"call_id": "c1"}))
        evt = FakeEvt(text, MASTER)
        _run(client.fn("handle_bare_number")(evt))
        assert evt.replies == [SMSErrorType.NUMBER_MALFORMED_CALL.value]
        assert posts == []

    def test_dispatch_filter(self, monkeypatch):
        # the NewMessage func decides what reaches the handler:
        # attempts (first char '+' or a digit) reach it and get either
        # a call or an error; plain text is not dispatched at all
        ub, client = make_ub(monkeypatch)
        f = client.spec("handle_bare_number")["func"]
        assert f(FakeEvt("+79991234567", MASTER)) is True
        assert f(FakeEvt("89991234567", MASTER)) is True
        assert f(FakeEvt("123", MASTER)) is True       # 3-digit service number
        assert f(FakeEvt("12345678", MASTER)) is True  # PIN: error, not silence
        assert f(FakeEvt("79991234567", MASTER)) is True
        assert f(FakeEvt("Привет 2026", MASTER)) is False
        assert f(FakeEvt("тел: +79991234567", MASTER)) is False
        assert f(FakeEvt(None, MASTER)) is False
