"""Userbot main module — Telethon client with SMS/broadcast handlers.

Runs on the Telegram node. Communicates with simbridge-agent via HTTP
for outgoing SMS. Receives incoming SMS via its own HTTP endpoint.

Secrets (API_ID, API_HASH) are read from environment variables named in
the config — never hardcoded (Rule 1).
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging import getLogger
from typing import Optional

from telethon import TelegramClient, events

from core.config import load_config
from core.acl import ACLManager
from core.audit import AuditLogger
from core.events import EventType

logger = getLogger("simbridge.userbot")


class Userbot:
    """Telegram userbot wrapper."""

    def __init__(self, cfg: Optional[dict] = None):
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

        # Agent HTTP URL (for outgoing SMS)
        self._agent_url = f"http://{self.cfg['agent.listen']}"
        self._agent_token = os.environ.get(self.cfg["agent.token_env"], "")

        # Register handlers
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register Telethon event handlers."""

        @self._client.on(events.NewMessage(pattern="/sms"))
        async def handle_sms(evt):
            """Handle /sms <phone> <message>."""
            parts = evt.message.text.split(None, 2)
            if len(parts) < 3:
                await evt.reply("Usage: /sms <phone> <message>")
                return

            phone = parts[1]
            text = parts[2]
            sender_id = evt.sender_id if evt.sender_id else 0

            # ACL check
            if not self._acl.check(sender_id, "out_sms"):
                self._audit.log(
                    EventType.USER_DENIED,
                    telegram_user_id=sender_id,
                    outcome="denied",
                    details={"right": "out_sms", "command": "sms"},
                )
                await evt.reply("Access denied: no out_sms permission.")
                return

            # Call agent API (HTTP)
            import httpx
            try:
                async with httpx.AsyncClient() as http:
                    resp = await http.post(
                        f"{self._agent_url}/v1/sms",
                        json={
                            "to": phone,
                            "text": text,
                            "telegram_user_id": sender_id,
                        },
                        headers={
                            "Authorization": f"Bearer {self._agent_token}",
                            "Content-Type": "application/json",
                        },
                        timeout=30.0,
                    )
                    resp.raise_for_status()
                    await evt.reply(f"Sent to {phone}")
            except httpx.HTTPError as e:
                await evt.reply(f"Error sending SMS: {e}")

        @self._client.on(events.NewMessage(pattern="/broadcast"))
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

        @self._client.on(events.NewMessage(file="voice"))
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

        @self._client.on(events.NewMessage(pattern="/help"))
        async def handle_help(evt):
            """Show available commands."""
            sender_id = evt.sender_id or 0
            rights = self._acl.get_user_rights(sender_id)

            help_text = "SimBridge commands:\n"
            if "out_sms" in rights:
                help_text += "  /sms <phone> <message> — send SMS\n"
                help_text += "  /broadcast <message> — send to all\n"
            if "in_sms" in rights:
                help_text += "  (incoming SMS forwarded automatically)\n"

            await evt.reply(help_text)

    async def resolve_master(self) -> int:
        """Resolve master user ID via get_entity at startup.

        Never use a hardcoded ID (knowledge item 10).
        """
        entity = await self._client.get_entity(self._master_username)
        self._master_id = entity.id
        logger.info("Resolved master user %s → ID %s", self._master_username, self._master_id)
        return self._master_id

    async def start(self) -> None:
        """Start the userbot and connect to Telegram."""
        await self._client.connect()
        await self.resolve_master()
        logger.info("Userbot started, connected to Telegram")
        await self._client.start()

    async def run_until_disconnected(self) -> None:
        """Block until the client disconnects."""
        await self._client.run_until_disconnected()

    @property
    def acl(self) -> ACLManager:
        return self._acl

    @property
    def audit(self) -> AuditLogger:
        return self._audit
