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
from typing import Optional

from telethon import TelegramClient, events

from core.config import load_config
from core.acl import ACLManager
from core.audit import AuditLogger
from core.contacts import ContactResolver
from core.blacklist import BlacklistManager
from core.phone import normalize_e164
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

# Pattern for explicit-number SMS: +79261234555: message
EXPLICIT_NUMBER_RE = re.compile(
    r"^(\+?\d[\d\s\-\(\)]{6,}\d)\s*:\s*(.+)?"
)

# S04.3: a message that IS a phone number (the entire text) is an
# outgoing-call request. Mutually exclusive with EXPLICIT_NUMBER_RE:
# the explicit form contains a colon, which is not in this charset.
BARE_NUMBER_RE = re.compile(r"^\+?\d[\d\s\-()]{6,}\d$")


def extract_call_request(text: Optional[str]) -> Optional[str]:
    """Return the target number if *text* is a bare phone number, else
    None. Pure function (S04.3): the whole message must be the number.

    Known false positive: an 8+ digit numeric string (a PIN-like code)
    matches and is treated as a call request — it simply rings that
    number. Accepted trade-off, documented in voice-bridge.md.
    """
    if not text:
        return None
    stripped = text.strip()
    if BARE_NUMBER_RE.match(stripped):
        return stripped
    return None


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

        # Register handlers
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register Telethon event handlers."""

        @self._client.on(events.NewMessage(pattern=r"^/sms\b"))
        async def handle_sms(evt):
            """Handle /sms <phone> <message> or reply to incoming SMS."""
            sender_id = evt.sender_id if evt.sender_id else 0

            # ACL check
            if not self._acl.check(sender_id, "out_sms"):
                self._audit.log(
                    EventType.USER_DENIED,
                    telegram_user_id=sender_id,
                    outcome="denied",
                    details={"right": "out_sms", "command": "sms"},
                )
                # S02.4: an ACL denial is a denial — not "number missing".
                await evt.reply(SMSErrorType.DENIED.value)
                return

            # S02.3: Check if this is a reply to an incoming SMS
            phone = None
            text = None

            if evt.is_reply_to and evt.reply_to_msg_id:
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
                        text = evt.message.text.strip()
                        # Strip the quoted reply part if present
                        if text.startswith("/sms"):
                            text = re.sub(r"^/sms\s+", "", text).strip()

            if not phone:
                # Not a reply, parse arguments
                parts = evt.message.text.split(None, 2)
                if len(parts) < 3:
                    await evt.reply("Usage: /sms <phone> <message>")
                    return
                phone = parts[1]
                text = parts[2]

            # Normalize phone number (S02.1)
            norm = normalize_e164(phone)
            if not norm:
                await evt.reply(SMSErrorType.NUMBER_MALFORMED.value)
                return

            # S02.2: Check blacklist
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

        @self._client.on(events.NewMessage(pattern=r"^/broadcast\b"))
        async def handle_broadcast(evt):
            """Handle /broadcast <message> — send to all out_sms users."""
            parts = evt.message.text.split(None, 1)
            if len(parts) < 2:
                await evt.reply("Usage: /broadcast <message>")
                return

            message = parts[1]
            sender_id = evt.sender_id or 0

            # ACL check
            if not self._acl.check(sender_id, "out_sms"):
                self._audit.log(
                    EventType.USER_DENIED,
                    telegram_user_id=sender_id,
                    outcome="denied",
                    details={"right": "out_sms", "command": "broadcast"},
                )
                await evt.reply(SMSErrorType.DENIED.value)
                return

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
                func=lambda e: extract_call_request(
                    e.message.text or ""
                )
                is not None
            )
        )
        async def handle_bare_number(evt):
            """S04.3: a message that IS a phone number (e.g.
            "+79261234555") is an outgoing-call request."""
            sender_id = evt.sender_id if evt.sender_id else 0
            phone = extract_call_request(evt.message.text or "")

            # S04.3: ACL before any call session.
            if not self._acl.check(sender_id, "out_call"):
                self._audit.log(
                    EventType.USER_DENIED,
                    telegram_user_id=sender_id,
                    outcome="denied",
                    details={"right": "out_call", "command": "call"},
                )
                await evt.reply(SMSErrorType.DENIED.value)
                return

            norm = normalize_e164(phone)
            if not norm:
                await evt.reply(SMSErrorType.NUMBER_MALFORMED.value)
                return

            # S02.2 parity: a blacklisted target is not dialed.
            if self._blacklist.contains(norm):
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

            # Ring the Telegram user through the bridge (S04.3). On
            # failure: reject the agent-side call — the reserved modem
            # must not sit in TELEGRAM_CALLING until the 30 s timeout.
            if not await self._bridge.start_call(sender_id, norm):
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
                await evt.reply("Ошибка: голосовой мост недоступен")
                return

            await evt.reply("Звоню вам в Telegram…")

        @self._client.on(events.NewMessage(func=lambda e: e.voice is not None))
        async def handle_voice_note(evt):
            """Handle incoming voice notes from Telegram."""
            sender_id = evt.sender_id or 0
            if not self._acl.check(sender_id, "in_call"):
                self._audit.log(
                    EventType.USER_DENIED,
                    telegram_user_id=sender_id,
                    outcome="denied",
                    details={"right": "in_call"},
                )
                return
            # TODO: download voice note, forward to agent for playback
            pass

        @self._client.on(events.NewMessage(pattern=r"^/block\b"))
        async def handle_block(evt):
            """Handle /block <phone> — add to blacklist (S02.2)."""
            parts = evt.message.text.split()
            if len(parts) < 2:
                await evt.reply("Usage: /block <phone>")
                return

            sender_id = evt.sender_id or 0

            # ACL: require out_sms (blocking is an admin-level action)
            if not self._acl.check(sender_id, "out_sms"):
                self._audit.log(
                    EventType.USER_DENIED,
                    telegram_user_id=sender_id,
                    outcome="denied",
                    details={"right": "out_sms", "command": "block"},
                )
                await evt.reply(SMSErrorType.DENIED.value)
                return

            phone = parts[1]
            norm = normalize_e164(phone)
            if not norm:
                await evt.reply(SMSErrorType.NUMBER_MALFORMED.value)
                return

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

        @self._client.on(events.NewMessage(pattern=r"^/unblock\b"))
        async def handle_unblock(evt):
            """Handle /unblock <phone> — remove from blacklist (S02.2)."""
            parts = evt.message.text.split()
            if len(parts) < 2:
                await evt.reply("Usage: /unblock <phone>")
                return

            sender_id = evt.sender_id or 0

            if not self._acl.check(sender_id, "out_sms"):
                self._audit.log(
                    EventType.USER_DENIED,
                    telegram_user_id=sender_id,
                    outcome="denied",
                    details={"right": "out_sms", "command": "unblock"},
                )
                await evt.reply(SMSErrorType.DENIED.value)
                return

            phone = parts[1]
            norm = normalize_e164(phone)
            if not norm:
                await evt.reply(SMSErrorType.NUMBER_MALFORMED.value)
                return

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

        @self._client.on(events.NewMessage(pattern=r"^/help\b"))
        async def handle_help(evt):
            """Show available commands."""
            sender_id = evt.sender_id or 0
            rights = self._acl.get_user_rights(sender_id)

            help_text = "SimBridge commands:\n"
            if "out_sms" in rights:
                help_text += "  /sms <phone> <message> — send SMS\n"
                help_text += "  /broadcast <message> — send to all\n"
                help_text += "  /block <phone> — block number\n"
                help_text += "  /unblock <phone> — unblock number\n"
            if "out_call" in rights:
                help_text += "  <phone> — voice call (send the number alone)\n"
            if "in_sms" in rights:
                help_text += "  (incoming SMS forwarded automatically)\n"

            await evt.reply(help_text)

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
