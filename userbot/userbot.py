"""Userbot main module — Telethon client with SMS/broadcast handlers.

Runs on the Telegram node. Communicates with simbridge-agent via HTTP
for outgoing SMS. Receives incoming SMS via its own HTTP endpoint.

S02 features:
- Contact name resolution for incoming SMS display
- BLOCK/UNBLOCK commands with persistence
- Reply routing: reply to incoming SMS sends to that number
- Error surfaces: localized, user-facing messages

Secrets (API_ID, API_HASH) are read from environment variables named in
the config — never hardcoded (Rule 1).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from logging import getLogger
from pathlib import Path
from typing import Dict, List, Optional

from telethon import TelegramClient, events

from core.config import load_config
from core.acl import ACLManager
from core.audit import AuditLogger
from core.contacts import ContactResolver
from core.blacklist import BlacklistManager
from core.phone import is_service_number, normalize_e164, parse_destination
from core.user_contacts import UserContactsStore, parse_vcard
from core.events import EventType
from core.errors import SMSErrorType
from core.recovery import BackoffReconnector
from userbot.bridge_control import BridgeControl

logger = getLogger("simbridge.userbot")

# S06.2: Telegram session reconnect parameters (module constants so
# tests can monkeypatch them; the AMI reconnect uses the same convention
# in agent/agent.py).
TG_RECONNECT_MIN_DELAY = 5.0
TG_RECONNECT_MAX_DELAY = 300.0
TG_RECONNECT_MAX_RETRIES = 10

# vCard phonebook import: a real phonebook export fits well under this;
# the cap guards against sending the bot a 100 MB file.
VCARD_IMPORT_MAX_BYTES = 1_000_000

# Pattern for explicit-number SMS: +79261234555: message
EXPLICIT_NUMBER_RE = re.compile(
    r"^(\+?\d[\d\s\-\(\)]{6,}\d)\s*:\s*(.+)?"
)

def is_number_attempt(text: Optional[str]) -> bool:
    """True if *text* looks like the user typed a phone number.

    Dispatch rule (msg #48): a stripped message starting with a digit
    or '+' is a number ATTEMPT and gets the strict validator's verdict
    (``core.phone.parse_destination``) — allowed forms dial, anything
    else gets "Неправильный номер" instead of silence or a wrong dial.
    Text starting with a letter is never an attempt ("Привет 2026"
    stays a plain message).

    Replaces the old BARE_NUMBER_RE/extract_call_request, which only
    matched 7+ digit strings: short numbers (a PIN, an extension) were
    silently ignored and 8-10 digit strings were dialed as foreign
    numbers. Pure function: no Telethon event involved.
    """
    if not text:
        return False
    s = text.strip()
    if not s:
        return False
    return s[0] == "+" or s[0].isdigit()


# ---------------------------------------------------------------------------
# Command classification (2026-08-22)
#
# A message starting with "/" is a command attempt; its first token
# (compared case-insensitively) decides which handler owns it. These are
# pure functions so the dispatch rules are unit-testable without a
# Telethon event (telethon is not installed in the test environment).
# ---------------------------------------------------------------------------

KNOWN_COMMANDS = frozenset({"sms", "broadcast", "block", "unblock", "listblock", "redial", "call", "contacts", "del", "help"})


def command_token(text: Optional[str]) -> Optional[str]:
    """Lowercase first token if *text* starts with "/", else None.

    "/SMS +7.. hi" -> "sms"; "/EEE x" -> "eee"; "988.." -> None;
    "/  " -> None (no token). Pure: no Telethon event involved.
    """
    if not text:
        return None
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    parts = stripped[1:].split(None, 1)
    # "/" or "/   " -> parts is [] (no token) — not a command attempt
    return parts[0].lower() if parts else None


def is_unknown_command(text: Optional[str]) -> bool:
    """True if *text* starts with "/" but is not a known command.

    Func-filter for the unknown-command handler. Mutually exclusive with
    the five command patterns by construction (they match exactly the
    KNOWN_COMMANDS tokens), so "/sms …" never reaches the unknown handler
    and vice versa — no double dispatch.
    """
    token = command_token(text)
    return token is not None and token not in KNOWN_COMMANDS


def is_plain_reply_candidate(text: Optional[str]) -> bool:
    """True if *text* is neither a command nor a number attempt.

    Part of the func-filter for the reply-to-SMS handler: a plain reply
    like "Ответ" qualifies, while "/sms …" (owned by the /sms handler)
    and "8926…" (owned by the call handler) do not — this is what
    prevents the same message from being dispatched twice.
    """
    return command_token(text) is None and not is_number_attempt(text)


class Userbot:
    """Telegram userbot wrapper."""

    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()

        # Secrets from environment — config only holds env var names
        api_id = os.environ[self.cfg["telegram.api_id_env"]]
        api_hash = os.environ[self.cfg["telegram.api_hash_env"]]

        self._client = TelegramClient(
            self.cfg["telegram.session_path"],
            api_id,
            api_hash,
            system_version="SimBridge/0.1",
        )

        # Master resolution via get_entity at startup (knowledge item 10)
        self._master_username = self.cfg["telegram.master_username"]
        self._master_id: Optional[int] = None

        # ACL
        self._acl = ACLManager(self.cfg["telegram.acl_file"])

        # Audit
        self._audit = AuditLogger(self.cfg["paths.audit_log"])

        # S02.1: Contact resolver
        self._contacts = ContactResolver(csv_path=self.cfg["paths.contacts_cache"])

        # S02.2: Blacklist manager
        self._blacklist = BlacklistManager(path=self.cfg["paths.blacklist"])

        # Agent HTTP URL (for outgoing SMS)
        self._agent_url = f"http://{self.cfg['agent.listen']}"
        self._agent_token = os.environ.get(self.cfg["agent.token_env"], "")

        # S04.3: voice bridge control API (loopback, same node)
        self._bridge = BridgeControl(self.cfg)

        # /redial: last dialed number per Telegram user, remembered on
        # successful agent call registration (see _place_call).
        # In-memory by design — after a restart the user simply dials
        # the number again.
        self._last_call: Dict[int, str] = {}

        # Per-user phonebooks (vCard import): dial-by-name (/call) and
        # per-recipient caller identification. Optional config key
        # paths.user_contacts; default — next to the global contacts
        # cache (same directory), logged at startup.
        uc_path = self.cfg.get("paths.user_contacts")
        if not uc_path:
            uc_path = str(
                Path(self.cfg["paths.contacts_cache"]).parent / "user_contacts.json"
            )
        self._user_contacts = UserContactsStore(uc_path)
        logger.info("User contacts store: %s", uc_path)

        # Register handlers
        self._register_handlers()

    def _access(self, sender_id: int, right: str) -> str:
        """Access verdict for *sender_id* against *right*.

        Returns:
          "ok"      — user has the right; proceed;
          "denied"  — user IS in the ACL but lacks the right: the caller
                      replies SMSErrorType.DENIED ("Недостаточно прав");
          "unknown" — user is NOT in the ACL at all: the caller stays
                      SILENT (no reply).

        The unknown-silence is the 2026-08-22 behavior the user
        requested: a sender who is not in the access list must get no
        reaction to any message — the bot must not confirm it is
        listening. Both non-ok verdicts are audit-logged locally; only
        "denied" produces a Telegram reply.
        """
        rights = self._acl.get_user_rights(sender_id)
        if not rights:
            self._audit.log(
                EventType.USER_DENIED,
                telegram_user_id=sender_id,
                outcome="unknown_user",
                details={"right": right},
            )
            return "unknown"
        if right not in rights:
            self._audit.log(
                EventType.USER_DENIED,
                telegram_user_id=sender_id,
                outcome="denied",
                details={"right": right},
            )
            return "denied"
        return "ok"

    def _is_known(self, sender_id: int) -> bool:
        """True if *sender_id* holds at least one ACL right (is in the list).

        A user line in acl.conf always carries at least one right (the
        parser skips right-less lines), so a non-empty right set is
        exactly "the user is in the access list".
        """
        return bool(self._acl.get_user_rights(sender_id))

    async def _place_call(self, evt, sender_id: int, dest) -> None:
        """Dial a validated *dest*: blacklist check, agent registration,
        bridge ring, user reply (S04.3).

        Shared by handle_bare_number (the user typed the number) and
        handle_redial (/redial — the remembered last number) — one
        submission path, one error vocabulary (Rule 1). The caller has
        passed the out_call access gate.

        On a successful agent registration *dest.number* is remembered
        as this user's last dialed number (the /redial source).
        """
        norm = dest.number

        # S02.2 parity: a blacklisted target is not dialed. The
        # blacklist holds carrier E.164 numbers — an internal
        # extension cannot match it.
        if not dest.is_internal and self._blacklist.contains(norm):
            await evt.reply(SMSErrorType.BLACKLISTED.value)
            return

        # Register the call with the agent (ACL re-check + atomic
        # modem reservation happen there).
        cid = uuid.uuid4().hex
        import httpx

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(
                    f"{self._agent_url}/v1/call/outgoing",
                    json={
                        "phone_number": norm,
                        "telegram_user_id": sender_id,
                    },
                    headers={
                        "Authorization": f"Bearer {self._agent_token}",
                        "Content-Type": "application/json",
                        "x-correlation-id": cid,
                    },
                    timeout=10.0,
                )
        except httpx.HTTPError as e:
            # The user gets a short message; the agent-side cause
            # (connection refused, timeout, 5xx) goes to the log —
            # without it the user sees "Сервис звонков недоступен"
            # with nothing to diagnose (live incident 2026-08-20,
            # Тест #1).
            logger.exception(
                "outgoing call registration failed (agent_url=%s): %s",
                self._agent_url, e,
            )
            await evt.reply("Сервис звонков недоступен")
            return

        if resp.status_code == 403:
            await evt.reply(SMSErrorType.BLACKLISTED.value)
            return
        if resp.status_code == 429:
            await evt.reply("Слишком много звонков. Попробуйте позже.")
            return
        if resp.status_code == 503:
            await evt.reply("Модем занят — другой звонок идёт.")
            return
        if resp.status_code >= 400:
            await evt.reply(SMSErrorType.SEND_FAILED.value)
            return

        try:
            call_id = resp.json().get("call_id", "")
        except ValueError:
            call_id = ""

        # /redial source: the number the agent accepted for dialing.
        self._last_call[sender_id] = norm

        # Ring the Telegram user through the bridge (S04.3). On a
        # non-"ok" outcome: reject the agent-side call — the reserved
        # modem must not sit in TELEGRAM_CALLING until the 30 s timeout.
        # "busy" (bridge already on another call) and "error" (bridge
        # down / unreachable) are different states and get different
        # messages — a busy bridge reported as "недоступен" sends the
        # user to re-dial into the same 503 (live 2026-10-05, 27c26ee1).
        result = await self._bridge.start_call(sender_id, norm)
        if result != "ok":
            try:
                async with httpx.AsyncClient() as http:
                    await http.post(
                        f"{self._agent_url}/v1/call/{call_id}/reject",
                        headers={
                            "Authorization": f"Bearer {self._agent_token}",
                            "Content-Type": "application/json",
                            "x-correlation-id": uuid.uuid4().hex,
                        },
                        timeout=10.0,
                    )
            except httpx.HTTPError as e:
                # The call will still expire via /call/check-timeouts
                # (TELEGRAM_CALLING window) — logged, not fatal.
                logger.warning(
                    "bridge start failed and agent reject failed: %s", e
                )
            if result == "busy":
                await evt.reply("Мост занят — другой звонок идёт.")
            else:
                await evt.reply("Ошибка: голосовой мост недоступен")
            return

        await evt.reply("Звоню вам в Telegram…")

    async def _do_send_sms(self, evt, phone: str, text: str,
                           sender_id: int) -> None:
        """Validate and submit one outgoing SMS; reply with the outcome.

        Shared by handle_sms (``/sms <phone> <text>``) and
        handle_sms_reply (a plain reply to a forwarded incoming SMS) —
        one submission path, one error vocabulary (Rule 1). The caller
        must have already passed the out_sms access check.
        """
        if not (text or "").strip():
            # Only reachable via the reply path (a "/sms" reply with no
            # body) — an empty SMS would waste a submission to a real
            # phone.
            await evt.reply("Пустое сообщение")
            return

        # Strict validation (msg #48): only '+'+11-15 digits, '8'+11-15
        # total, 3-digit local service (callable, not sendable), or
        # 4-digit internal. Internal extensions and service numbers
        # cannot receive SMS; formatted/short-foreign forms are
        # rejected here instead of being silently normalized as before.
        dest = parse_destination(phone)
        if dest is None or dest.is_internal or is_service_number(dest.number):
            await evt.reply(SMSErrorType.NUMBER_MALFORMED_SMS.value)
            return
        norm = dest.number

        # S02.2: a blacklisted target is not sent to.
        if self._blacklist.contains(norm):
            await evt.reply(SMSErrorType.BLACKLISTED.value)
            return

        # Call agent API (HTTP) with correlation (S02.3).
        # The x-correlation-id header activates the agent's replay
        # protection (S01.3); the same id is sent in the JSON body for
        # audit tracing.
        cid = uuid.uuid4().hex
        import httpx

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(
                    f"{self._agent_url}/v1/sms",
                    json={
                        "to": norm,
                        "text": text,
                        "telegram_user_id": sender_id,
                        "telegram_message_id": evt.message.id,
                        "correlation_id": cid,
                    },
                    headers={
                        "Authorization": f"Bearer {self._agent_token}",
                        "Content-Type": "application/json",
                        "x-correlation-id": cid,
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()
                await evt.reply("Отправлено")
        except httpx.HTTPStatusError as e:
            # S02.4: Map HTTP errors to user-friendly messages.
            # The agent returns {"detail": "<localized>"} on 4xx/5xx
            # (e.g. the categorized 502 message) — prefer it over
            # the raw body.
            try:
                detail = e.response.json().get("detail") or ""
            except (ValueError, AttributeError):
                detail = e.response.text
            if e.response.status_code == 403:
                await evt.reply(SMSErrorType.BLACKLISTED.value)
            elif e.response.status_code == 429:
                await evt.reply("Слишком много SMS. Попробуйте позже.")
            else:
                await evt.reply(
                    f"Ошибка отправки: {detail}" if detail
                    else SMSErrorType.SEND_FAILED.value
                )
        except httpx.HTTPError as e:
            await evt.reply(SMSErrorType.MODEM_UNAVAILABLE.value)

    def _register_handlers(self) -> None:
        """Register Telethon event handlers."""

        @self._client.on(events.NewMessage(pattern=r"(?i)^/sms\b"))
        async def handle_sms(evt):
            """Handle /sms <phone> <message> (case-insensitive, 2026-08-22)
            or a /sms-prefixed reply to an incoming SMS."""
            sender_id = evt.sender_id if evt.sender_id else 0

            # Access gate FIRST (2026-08-22): unknown sender -> silence,
            # known sender without out_sms -> "Недостаточно прав". No
            # parsing or submission happens before this verdict.
            verdict = self._access(sender_id, "out_sms")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            # S02.3: a /sms-prefixed reply to an incoming SMS carries the
            # number in the quoted message, not in the text.
            phone = None
            text = None

            # Telethon 1.44: NewMessage proxies unknown attributes to
            # evt.message; Message has is_reply (no is_reply_to).
            if evt.is_reply and evt.reply_to_msg_id:
                # Reply to an incoming SMS — get the number from the message
                reply_to = await evt.get_reply_message()
                if reply_to:
                    # Extract phone from the incoming SMS format:
                    # "SMS +79261234555 (Name): message" or "SMS +79261234555: message"
                    sms_match = re.match(
                        r"SMS\s+(\+?\d+)", reply_to.text or ""
                    )
                    if sms_match:
                        phone = sms_match.group(1)
                        raw = (evt.message.text or "").strip()
                        # Strip the (case-insensitive) command prefix if
                        # the reply re-quotes it, e.g. "/SMS Ответ".
                        m = re.match(r"(?i)^/sms\s*", raw)
                        text = raw[m.end():].strip() if m else raw

            if not phone:
                # Not a reply, parse arguments
                parts = evt.message.text.split(None, 2)
                if len(parts) < 3:
                    await evt.reply("Usage: /sms <phone> <message>")
                    return
                phone = parts[1]
                text = parts[2]

            await self._do_send_sms(evt, phone, text, sender_id)

        @self._client.on(events.NewMessage(pattern=r"(?i)^/broadcast\b"))
        async def handle_broadcast(evt):
            """Handle /broadcast <message> — send to all out_sms users
            (case-insensitive, 2026-08-22)."""
            sender_id = evt.sender_id or 0

            # Access gate FIRST (2026-08-22): unknown -> silence,
            # known without out_sms -> "Недостаточно прав". The usage
            # hint is only shown to users who passed the gate.
            verdict = self._access(sender_id, "out_sms")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            parts = evt.message.text.split(None, 1)
            if len(parts) < 2:
                await evt.reply("Usage: /broadcast <message>")
                return

            message = parts[1]

            # D13: send the raw text to every user with out_sms,
            # including the sender. Per-user isolation.
            recipients = sorted(self._acl.users_with_right("out_sms"))
            sent: list[int] = []
            failed: list[int] = []
            for uid in recipients:
                try:
                    await self._client.send_message(uid, message)
                    sent.append(uid)
                except Exception as e:
                    failed.append(uid)
                    logger.warning("broadcast: user %s failed: %s", uid, e)
            self._audit.log(
                EventType.BROADCAST_SENT,
                telegram_user_id=sender_id,
                outcome="ok" if not failed else "partial",
                details={
                    "recipients": recipients,
                    "delivered_to": sent,
                    "text_len": len(message),
                },
            )
            await evt.reply(
                f"Рассылка: доставлено {len(sent)} из {len(recipients)}"
            )

        @self._client.on(
            events.NewMessage(
                func=lambda e: is_number_attempt(e.message.text or "")
            )
        )
        async def handle_bare_number(evt):
            """S04.3: a message that looks like a phone number (e.g.
            "+79261234555", "89261234555", "100") is an outgoing-call
            attempt. Strict validation (msg #48) decides: allowed forms
            dial, everything else gets "Неправильный номер"."""
            sender_id = evt.sender_id if evt.sender_id else 0

            # S04.3: access before any call session (2026-08-22):
            # unknown -> silence, known without out_call -> "Недостаточно
            # прав". No dialing, no agent registration before this.
            verdict = self._access(sender_id, "out_call")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            dest = parse_destination((evt.message.text or "").strip())
            if dest is None:
                await evt.reply(SMSErrorType.NUMBER_MALFORMED_CALL.value)
                return
            await self._place_call(evt, sender_id, dest)

        @self._client.on(events.NewMessage(pattern=r"(?i)^/redial\b"))
        async def handle_redial(evt):
            """/redial — redial this user's last dialed number.

            The number is remembered when the agent accepts an outgoing
            call (see _place_call); the memory is per Telegram user and
            in-memory only (lost on restart — the user dials again).
            Same access gate and error path as a bare-number call.
            """
            sender_id = evt.sender_id if evt.sender_id else 0
            verdict = self._access(sender_id, "out_call")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            stored = self._last_call.get(sender_id)
            if stored is None:
                await evt.reply("Нет последнего номера для повтора")
                return
            # Re-validate through the same strict parser: the stored
            # number is always a parseable form (it was dialed through
            # it), this guards the invariant explicitly.
            dest = parse_destination(stored)
            if dest is None:
                await evt.reply(SMSErrorType.NUMBER_MALFORMED_CALL.value)
                return
            await self._place_call(evt, sender_id, dest)

        @self._client.on(
            events.NewMessage(
                func=lambda e: (
                    (e.message.file is not None and not e.voice)
                    or e.contact is not None
                )
            )
        )
        async def handle_contact_file(evt):
            """Directory import, two forms (same out_call gate):

            - a DOCUMENT (.vcf phonebook export): the user's directory
              is REPLACED — a file is a full phonebook;
            - a FORWARDED CONTACT card (MessageMediaContact): ONE entry
              is upserted (added or updated by number) — the user can
              forward contacts one by one.

            Every outcome is logged (2026-09-28: an import that
            produced neither a reply the user remembered nor a log
            line is a black box) and always ends in a user reply.
            """
            sender_id = evt.sender_id if evt.sender_id else 0
            verdict = self._access(sender_id, "out_call")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            contact = getattr(evt, "contact", None)
            if contact is not None:
                # Telethon 1.44 TL schema: MessageMediaContact exposes
                # phone_number / first_name / last_name / vcard / user_id
                # (there is no .phone attribute — reading it always gave
                # None, so every card looked number-less). The vcard field
                # carries the full vCard the client shared.
                phone = (getattr(contact, "phone_number", None)
                         or getattr(contact, "phone", None))
                vcard = getattr(contact, "vcard", None)
                name = " ".join(
                    p for p in (
                        getattr(contact, "first_name", None),
                        getattr(contact, "last_name", None),
                    ) if p
                ).strip()
                vcard_head = (
                    vcard[:400] + "…" if vcard and len(vcard) > 400 else vcard
                )
                logger.info(
                    "contact import: user %s card fields: "
                    "phone_number=%r first=%r last=%r user_id=%r vcard=%r",
                    sender_id,
                    getattr(contact, "phone_number", None),
                    getattr(contact, "first_name", None),
                    getattr(contact, "last_name", None),
                    getattr(contact, "user_id", None),
                    vcard_head,
                )
                # The structured field can be empty while the embedded
                # vCard still carries the number — recover it from there.
                if not phone and vcard:
                    entries = parse_vcard(vcard)
                    if entries:
                        phone = entries[0]["number"]
                        if not name:
                            name = entries[0]["name"]
                if not phone:
                    logger.warning("contact import: user %s card has no "
                                   "number (phone_number and vcard both "
                                   "empty; name: %r)", sender_id,
                                   name or "<none>")
                    await evt.reply(
                        f"У этого контакта нет номера"
                        + (f" ({name})." if name else ".")
                        + " Проверьте номер в контакте в телефоне "
                          "или отправьте файл .vcf."
                    )
                    return
                if not name:
                    name = f"Контакт {phone}"
                total = self._user_contacts.upsert_for(
                    sender_id,
                    {"name": name, "number": normalize_e164(phone) or phone},
                )
                logger.info("contact import: user %s saved %s (%s), "
                            "total %d", sender_id, name, phone, total)
                await evt.reply(f"Сохранено: {name} ({phone}). "
                                f"В справочнике: {total}.")
                return

            size = getattr(evt.message.file, "size", None)
            logger.info("contact import: user %s sent a document (%s bytes)",
                        sender_id, size)
            if size is not None and size > VCARD_IMPORT_MAX_BYTES:
                logger.warning("contact import: user %s document too large "
                               "(%s bytes)", sender_id, size)
                await evt.reply("Файл слишком большой (макс. 1 МБ)")
                return
            try:
                data = await evt.message.download_bytes()
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("vCard import: unreadable document: %s", e)
                await evt.reply("Не удалось прочитать файл как vCard")
                return

            if "BEGIN:VCARD" not in text:
                logger.warning("contact import: user %s document is not a "
                               "vCard (%d chars)", sender_id, len(text))
                await evt.reply("Это не vCard-файл. Пришлите экспорт "
                                "телефонной книги (.vcf) или перешлите "
                                "контакт из своих контактов.")
                return
            entries = parse_vcard(text)
            if not entries:
                logger.warning("contact import: user %s vCard parsed to 0 "
                               "entries (%d chars)", sender_id, len(text))
                await evt.reply("vCard не распознан: нет контактов с "
                                "именем и номером.")
                return
            n = self._user_contacts.replace_for(sender_id, entries)
            self._audit.log(
                EventType.CONFIG_RELOADED,
                telegram_user_id=sender_id,
                outcome="ok",
                details={"kind": "user_contacts_import", "entries": n},
            )
            await evt.reply(f"Справочник заменён: {n} контактов.")

        @self._client.on(events.NewMessage(pattern=r"(?i)^/call\b"))
        async def handle_call(evt):
            """/call <name|number> — dial from the user's directory.

            A parseable number dials directly (same path as a bare
            number); anything else is a name search in the user's own
            vCard directory: one match dials, several are listed (the
            user then sends the number), none gets a hint to import.
            """
            sender_id = evt.sender_id if evt.sender_id else 0
            verdict = self._access(sender_id, "out_call")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            raw = (evt.message.text or "").strip()
            raw = re.sub(r"(?i)^/call\b\s*", "", raw)
            if not raw:
                await evt.reply("Использование: /call <имя или номер>")
                return

            dest = parse_destination(raw)
            if dest is None:
                matches = self._user_contacts.find_by_name(sender_id, raw)
                if not matches:
                    await evt.reply(
                        f"Не найдено: {raw}\n"
                        "Пришлите vCard-файл телефонной книги, "
                        "чтобы создать справочник."
                    )
                    return
                if len(matches) > 1:
                    lines = [f"{i}) {m['name']}  {m['number']}"
                             for i, m in enumerate(matches[:10], 1)]
                    more = "" if len(matches) <= 10 else f"\n… и ещё {len(matches) - 10}"
                    await evt.reply("Несколько совпадений:\n" + "\n".join(lines)
                                    + more + "\nНаберите номер, чтобы позвонить.")
                    return
                dest = parse_destination(matches[0]["number"])
                if dest is None:
                    await evt.reply(f"Номер в справочнике некорректен: "
                                    f"{matches[0]['number']}")
                    return
                logger.info("/call: user %s dialed %r -> %s",
                            sender_id, raw, dest.number)
            await self._place_call(evt, sender_id, dest)

        @self._client.on(events.NewMessage(pattern=r"(?i)^/contacts\b"))
        async def handle_contacts(evt):
            """/contacts — show this user's imported directory (out_call)."""
            sender_id = evt.sender_id if evt.sender_id else 0
            verdict = self._access(sender_id, "out_call")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            entries = self._user_contacts.get(sender_id)
            if not entries:
                await evt.reply("Справочник пуст. Пришлите vCard-файл "
                                "телефонной книги (.vcf) или перешлите "
                                "контакт из своих контактов.")
                return
            lines = [f"{e['name']}  {e['number']}" for e in entries[:50]]
            more = f"\n… и ещё {len(entries) - 50}" if len(entries) > 50 else ""
            await evt.reply(f"Справочник ({len(entries)}):\n" + "\n".join(lines) + more)

        @self._client.on(events.NewMessage(pattern=r"(?i)^/del\b"))
        async def handle_del(evt):
            """/del <name|number> — remove entries from the user's directory.

            A parseable number deletes by exact (E.164-normalized)
            number — unambiguous. A name deletes only on a unique
            case-insensitive match; ambiguous matches are listed so the
            user deletes by number. Destructive by design, hence the
            strict matching. Full reset = send a new vCard file
            (import replaces the directory).
            """
            sender_id = evt.sender_id if evt.sender_id else 0
            verdict = self._access(sender_id, "out_call")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            raw = re.sub(r"(?i)^/del\b\s*", "", (evt.message.text or "").strip())
            if not raw:
                await evt.reply("Использование: /del <имя или номер>")
                return

            dest = parse_destination(raw)
            if dest is not None:
                n = self._user_contacts.delete_by_number(sender_id, dest.number)
                if n:
                    await evt.reply(f"Удалено: {n} (номер {dest.number})")
                else:
                    await evt.reply(f"Не найден номер {dest.number}")
                return

            matches = self._user_contacts.find_by_name(sender_id, raw)
            if not matches:
                await evt.reply(f"Не найдено: {raw}")
                return
            if len(matches) > 1:
                lines = [f"{i}) {m['name']}  {m['number']}"
                         for i, m in enumerate(matches[:10], 1)]
                await evt.reply("Несколько совпадений:\n" + "\n".join(lines)
                                + "\nУдалите по номеру: /del <номер>")
                return
            n = self._user_contacts.delete_by_name(sender_id, matches[0]["name"])
            if n:
                await evt.reply(f"Удалено: {matches[0]['name']} "
                                f"({matches[0]['number']})")
            else:
                await evt.reply("Не удалось удалить (запись не найдена)")

        @self._client.on(events.NewMessage(func=lambda e: e.voice is not None))
        async def handle_voice_note(evt):
            """Handle incoming voice notes from Telegram."""
            sender_id = evt.sender_id or 0
            # 2026-08-22: no reply in either verdict (voice notes have no
            # user-facing error channel here yet), but _access keeps the
            # audit accurate (unknown_user vs denied).
            self._access(sender_id, "in_call")
            # TODO: download voice note, forward to agent for playback
            pass

        @self._client.on(events.NewMessage(pattern=r"(?i)^/block\b"))
        async def handle_block(evt):
            """Handle /block <phone> — add to blacklist (S02.2,
            case-insensitive 2026-08-22)."""
            sender_id = evt.sender_id or 0

            # Access gate FIRST (2026-08-22): unknown -> silence,
            # known without out_sms -> "Недостаточно прав".
            verdict = self._access(sender_id, "out_sms")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            parts = evt.message.text.split()
            if len(parts) < 2:
                await evt.reply("Usage: /block <phone>")
                return

            # Strict validation (msg #48); internal extensions and
            # 3-digit service numbers are not carrier numbers, so they
            # cannot be blocked.
            dest = parse_destination(parts[1])
            if dest is None or dest.is_internal or is_service_number(dest.number):
                await evt.reply(SMSErrorType.NUMBER_MALFORMED.value)
                return
            norm = dest.number

            # Call agent API to persist the block. The x-correlation-id
            # header activates the agent's replay protection (S01.3).
            cid = uuid.uuid4().hex
            import httpx
            try:
                async with httpx.AsyncClient() as http:
                    resp = await http.post(
                        f"{self._agent_url}/v1/blacklist",
                        json={"number": norm},
                        headers={
                            "Authorization": f"Bearer {self._agent_token}",
                            "Content-Type": "application/json",
                            "x-correlation-id": cid,
                        },
                        timeout=10.0,
                    )
                    resp.raise_for_status()
                    # Also update local blacklist
                    self._blacklist.block(norm)
                    await evt.reply(f"Number {norm} blocked.")
            except httpx.HTTPError as e:
                await evt.reply(f"Error blocking number: {e}")

        @self._client.on(events.NewMessage(pattern=r"(?i)^/unblock\b"))
        async def handle_unblock(evt):
            """Handle /unblock <phone> — remove from blacklist (S02.2,
            case-insensitive 2026-08-22)."""
            sender_id = evt.sender_id or 0

            # Access gate FIRST (2026-08-22): unknown -> silence,
            # known without out_sms -> "Недостаточно прав".
            verdict = self._access(sender_id, "out_sms")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            parts = evt.message.text.split()
            if len(parts) < 2:
                await evt.reply("Usage: /unblock <phone>")
                return

            # Strict validation (msg #48); internal extensions and
            # 3-digit service numbers are not carrier numbers, so they
            # cannot be on the blacklist.
            dest = parse_destination(parts[1])
            if dest is None or dest.is_internal or is_service_number(dest.number):
                await evt.reply(SMSErrorType.NUMBER_MALFORMED.value)
                return
            norm = dest.number

            # The x-correlation-id header activates the agent's replay
            # protection (S01.3).
            cid = uuid.uuid4().hex
            import httpx
            try:
                async with httpx.AsyncClient() as http:
                    resp = await http.post(
                        f"{self._agent_url}/v1/unblock",
                        json={"number": norm},
                        headers={
                            "Authorization": f"Bearer {self._agent_token}",
                            "Content-Type": "application/json",
                            "x-correlation-id": cid,
                        },
                        timeout=10.0,
                    )
                    resp.raise_for_status()
                    self._blacklist.unblock(norm)
                    await evt.reply(f"Number {norm} unblocked.")
            except httpx.HTTPError as e:
                await evt.reply(f"Error unblocking number: {e}")

        @self._client.on(events.NewMessage(pattern=r"(?i)^/listblock\b"))
        async def handle_listblock(evt):
            """Handle /listblock — show the full blacklist (S02.2).

            Same access gate as /block and /unblock (out_sms): unknown
            -> silence, known without out_sms -> "Недостаточно прав".
            """
            sender_id = evt.sender_id or 0
            verdict = self._access(sender_id, "out_sms")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return

            numbers = self._blacklist.list()
            if not numbers:
                await evt.reply("Blacklist is empty.")
                return
            lines = [f"Blacklist ({len(numbers)}):"]
            lines.extend(f"  {n}" for n in numbers)
            await evt.reply("\n".join(lines))

        @self._client.on(events.NewMessage(pattern=r"(?i)^/help\b"))
        async def handle_help(evt):
            """Show available commands (known ACL users only)."""
            sender_id = evt.sender_id or 0
            # 2026-08-22: unknown users get total silence — /help must
            # not confirm the bot exists or list commands to strangers.
            if not self._is_known(sender_id):
                return
            rights = self._acl.get_user_rights(sender_id)

            help_text = "SimBridge commands:\n"
            if "out_sms" in rights:
                help_text += "  /sms <phone> <message> — send SMS\n"
                help_text += "  /broadcast <message> — send to all\n"
                help_text += "  /block <phone> — block number\n"
                help_text += "  /unblock <phone> — unblock number\n"
                help_text += "  /listblock — show blocked numbers\n"
            if "out_call" in rights:
                help_text += "  <phone> — voice call (send the number alone)\n"
                help_text += "  /redial — redial the last number\n"
                help_text += "  /call <name|number> — call from your directory\n"
                help_text += "  /contacts — show your directory\n"
                help_text += "  /del <name|number> — remove from your directory\n"
                help_text += "  (send a vCard file or forward a contact to import)\n"
            if "in_sms" in rights:
                help_text += "  (incoming SMS forwarded automatically)\n"

            await evt.reply(help_text)

        @self._client.on(
            events.NewMessage(
                func=lambda e: not e.voice
                and is_unknown_command(e.message.text or "")
            )
        )
        async def handle_unknown_command(evt):
            """Unknown command (/EEE ...) — "Неизвестная команда" to known
            users, total silence to unknown ones (2026-08-22).

            func filter, not a pattern: "starts with / but is NOT one of
            the KNOWN_COMMANDS tokens" cannot be expressed as the pattern
            of one known command. The filters are mutually exclusive:
            pattern handlers match only KNOWN_COMMANDS tokens, and
            handle_sms_reply excludes every "/"-prefixed text — no double
            dispatch (see is_unknown_command / is_plain_reply_candidate).
            """
            sender_id = evt.sender_id or 0
            if not self._is_known(sender_id):
                self._audit.log(
                    EventType.USER_DENIED,
                    telegram_user_id=sender_id,
                    outcome="unknown_user",
                    details={
                        "reason": "unknown_command",
                        "text": (evt.message.text or "")[:20],
                    },
                )
                return
            await evt.reply(
                f"{SMSErrorType.UNKNOWN_COMMAND.value}. /help — список команд."
            )

        @self._client.on(
            events.NewMessage(
                func=lambda e: (
                    e.reply_to_msg_id is not None
                    and not e.voice
                    and (e.message.text or "").strip()
                    and is_plain_reply_candidate(e.message.text or "")
                )
            )
        )
        async def handle_sms_reply(evt):
            """A plain reply (no /sms prefix) to a forwarded incoming SMS
            sends the reply text to that number (2026-08-22: user replied
            "Ответ" to the "Вход" SMS message and nothing was sent).

            Only replies to messages in the exact format we forward
            incoming SMS in ("SMS +7...") are acted on; a reply to any
            other message is left silent. The access gate runs before
            any parsing or reply (see _access).
            """
            sender_id = evt.sender_id or 0
            verdict = self._access(sender_id, "out_sms")
            if verdict != "ok":
                if verdict == "denied":
                    await evt.reply(SMSErrorType.DENIED.value)
                return
            reply_to = await evt.get_reply_message()
            if not reply_to:
                return
            sms_match = re.match(r"SMS\s+(\+?\d+)", reply_to.text or "")
            if not sms_match:
                return
            await self._do_send_sms(
                evt, sms_match.group(1), (evt.message.text or "").strip(), sender_id
            )

    async def resolve_master(self) -> int:
        """Resolve master user ID via get_entity at startup.

        Never use a hardcoded ID (knowledge item 10).
        """
        entity = await self._client.get_entity(self._master_username)
        self._master_id = entity.id
        logger.info("Resolved master user %s -> ID %s", self._master_username, self._master_id)
        return self._master_id

    async def start(self) -> None:
        """Start the userbot and connect to Telegram.

        start() must come BEFORE resolve_master() — resolve_master() calls
        get_entity() which requires an authenticated session.
        """
        await self._client.start()
        await self.resolve_master()
        logger.info("Userbot started, connected to Telegram")

    async def run_until_disconnected(self) -> None:
        """Block until the client disconnects."""
        await self._client.run_until_disconnected()

    async def run_with_recovery(self, alerts=None) -> None:
        """S06.2: run until the Telegram session is unrecoverable.

        ``run_until_disconnected()`` returns on EVERY session drop —
        a transient network glitch included. Exitting on the first drop
        would page the user (and require a manual restart) for blips
        that reconnect cleanly, so instead:

        1. alert the master via the passed AlertManager (if any),
        2. reconnect with exponential backoff (up to
           TG_RECONNECT_MAX_RETRIES attempts),
        3. continue running on success — the handlers registered in
           ``_register_handlers`` live on the client object and
           survive the disconnect/reconnect cycle.

        Returns only when the retries are exhausted: at that point the
        session is most likely invalidated (revoked auth, 2FA change),
        and the correct response is process exit → systemd restart →
        the re-auth flow documented in ``docs/re-auth.md``.

        Note (Rule 2): Telethon's exact ``connect()`` semantics after
        ``run_until_disconnected()`` cannot be verified in this
        environment (the library is not installed) — the reconnect
        cycle is a MANUAL_VERIFY item (TS06-9).
        """
        while True:
            await self.run_until_disconnected()
            logger.error("Telegram session dropped — attempting reconnect")
            if alerts is not None:
                try:
                    await alerts.alert(
                        "telegram_session_invalid",
                        "Telegram session dropped",
                    )
                except Exception:  # noqa: BLE001 — alerting must not block recovery
                    logger.debug("session-drop alert failed", exc_info=True)
            try:
                ok = await BackoffReconnector(
                    operation=self._client.connect,
                    label="Telegram session",
                    min_delay=TG_RECONNECT_MIN_DELAY,
                    max_delay=TG_RECONNECT_MAX_DELAY,
                    max_retries=TG_RECONNECT_MAX_RETRIES,
                ).start()
            except Exception as e:
                ok = False
                logger.error("Telegram reconnect raised: %s", e)
            if not ok:
                logger.critical(
                    "Telegram reconnect failed after %d attempts — session "
                    "may be invalid; exiting for the systemd re-auth restart "
                    "(see docs/re-auth.md)",
                    TG_RECONNECT_MAX_RETRIES,
                )
                return
            logger.info("Telegram session re-established")

    @property
    def client(self):
        """Telethon client (exposed for the in-process HTTP server, D1)."""
        return self._client

    @property
    def master_id(self) -> Optional[int]:
        """Master user's Telegram ID, resolved in ``resolve_master()``.

        Exposed for the HTTP server (S06.2: /events/alert forwards
        agent alerts to this ID) and for local alert sends.
        """
        return self._master_id

    @property
    def acl(self) -> ACLManager:
        return self._acl

    @property
    def audit(self) -> AuditLogger:
        return self._audit

    @property
    def contacts(self) -> ContactResolver:
        return self._contacts

    @property
    def user_contacts(self) -> UserContactsStore:
        return self._user_contacts

    @property
    def blacklist(self) -> BlacklistManager:
        return self._blacklist

    def format_incoming_sms(self, phone: str, text: str) -> str:
        """Format an incoming SMS message with contact name (S02.1).

        Format: "SMS +79261234555 (Иванов И.И.): message"
        Or:     "SMS +79261234555: message" (if no name)
        """
        norm = normalize_e164(phone) or phone
        name = self._contacts.resolve(norm)
        if name:
            return f"SMS {norm} ({name}):\n{text}"
        return f"SMS {norm}:\n{text}"
