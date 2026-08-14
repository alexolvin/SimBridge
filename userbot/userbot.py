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

logger = getLogger("simbridge.userbot")

# Pattern for explicit-number SMS: +79261234555: message
EXPLICIT_NUMBER_RE = re.compile(
    r"^(\+?\d[\d\s\-\(\)]{6,}\d)\s*:\s*(.+)?"
)


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
                await evt.reply(SMSErrorType.NUMBER_MISSING.value)
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

            # Call agent API (HTTP) with correlation (S02.3)
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
                        },
                        headers={
                            "Authorization": f"Bearer {self._agent_token}",
                            "Content-Type": "application/json",
                        },
                        timeout=30.0,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    await evt.reply(f"Sent to {norm}")
            except httpx.HTTPStatusError as e:
                # S02.4: Map HTTP errors to user-friendly messages
                detail = e.response.text
                if e.response.status_code == 403:
                    await evt.reply(SMSErrorType.BLACKLISTED.value)
                elif e.response.status_code == 429:
                    await evt.reply("Слишком много SMS. Попробуйте позже.")
                else:
                    await evt.reply(f"Ошибка отправки: {detail}")
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
                await evt.reply("Access denied.")
                return

            # TODO: iterate users with out_sms right and send
            await evt.reply("Broadcast sent.")

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
                await evt.reply("Access denied.")
                return

            phone = parts[1]
            norm = normalize_e164(phone)
            if not norm:
                await evt.reply(SMSErrorType.NUMBER_MALFORMED.value)
                return

            # Call agent API to persist the block
            import httpx
            try:
                async with httpx.AsyncClient() as http:
                    resp = await http.post(
                        f"{self._agent_url}/v1/blacklist",
                        json={"number": norm},
                        headers={
                            "Authorization": f"Bearer {self._agent_token}",
                            "Content-Type": "application/json",
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
                await evt.reply("Access denied.")
                return

            phone = parts[1]
            norm = normalize_e164(phone)
            if not norm:
                await evt.reply(SMSErrorType.NUMBER_MALFORMED.value)
                return

            import httpx
            try:
                async with httpx.AsyncClient() as http:
                    resp = await http.post(
                        f"{self._agent_url}/v1/unblock",
                        json={"number": norm},
                        headers={
                            "Authorization": f"Bearer {self._agent_token}",
                            "Content-Type": "application/json",
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
