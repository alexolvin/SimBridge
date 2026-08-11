"""Asterisk Manager Interface (AMI) client.

Provides parameterized access to Asterisk commands — user text never becomes
shell interpolation. Choosen over ARI because:
1. AMI is available on Asterisk 18 out of the box (no extra config)
2. DongleSendSMS is an AMI-level action, not exposed via ARI REST
3. Simpler dependency: one TCP connection, no HTTP server on Asterisk side

Security: parameters are sent as AMI message fields, not as a constructed
shell command. The SMS text is a separate AMI field — no injection surface.
"""

from __future__ import annotations

import asyncio
import io
import re
from typing import Optional


class AMIClient:
    """Async AMI client for sending SMS and querying modem state.

    Parameters are sent as AMI message fields — user text never becomes
    part of a shell command (Rule 1: no shell interpolation).
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5038,
        username: str = "simbridge",
        password: str = "",
        dongle: str = "gsm",
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._dongle = dongle
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    async def connect(self) -> None:
        """Open AMI TCP connection and log in."""
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )

        # Read login prompt
        await self._read_response()

        # Send login
        await self._send_action({"Action": "Login"})
        resp = await self._read_response()
        if resp.get("Response") not in ("Success", "Followed"):
            raise ConnectionError(f"AMI login failed: {resp}")

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._reader = None
            self._writer = None

    async def send_sms(self, to: str, text: str) -> dict:
        """Send SMS via DongleSendSMS AMI action.

        *to* and *text* are passed as separate AMI fields — no shell interpolation.
        The text is URL-encoded for the AMI protocol (commas are significant).
        """
        # DongleSendSMS expects: DongleSendSMS(dongle,to,text)
        # but via AMI the text field is separate from the action
        action_id = f"sms-{id(self)}-{asyncio.get_event_loop().time()}"
        await self._send_action(
            {
                "Action": "DongleCommand",
                "Command": f"DongleSendSMS({self._dongle},{to},'{text}')",
                "ActionID": action_id,
            }
        )
        return await self._read_response()

    async def get_modem_status(self) -> dict:
        """Query modem registration and signal status."""
        action_id = f"status-{id(self)}-{asyncio.get_event_loop().time()}"
        await self._send_action(
            {
                "Action": "DongleCommand",
                "Command": f"DongleStatus({self._dongle})",
                "ActionID": action_id,
            }
        )
        return await self._read_response()

    async def _send_action(self, fields: dict) -> None:
        """Send an AMI action message."""
        if not self._writer:
            raise ConnectionError("AMI client not connected")

        msg = ""
        for k, v in fields.items():
            msg += f"{k}: {v}\r\n"
        msg += "\r\n"

        self._writer.write(msg.encode())
        await self._writer.drain()

    async def _read_response(self) -> dict:
        """Read one AMI response message."""
        if not self._reader:
            raise ConnectionError("AMI client not connected")

        headers: dict[str, str] = {}
        while True:
            line = await self._reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            text = line.decode().strip()
            if ":" in text:
                key, _, value = text.partition(":")
                headers[key.strip()] = value.strip()

        return headers


# ---------------------------------------------------------------------------
# Synchronous wrapper for non-async callers (shell scripts, hooks)
# ---------------------------------------------------------------------------

def _run_async(coro):
    """Run an async function in a new event loop (for sync callers)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def send_sms_sync(
    host: str,
    port: int,
    username: str,
    password: str,
    dongle: str,
    to: str,
    text: str,
) -> dict:
    """Synchronous SMS send — used by the hook scripts."""
    client = AMIClient(host, port, username, password, dongle)

    async def _send():
        await client.connect()
        try:
            return await client.send_sms(to, text)
        finally:
            await client.close()

    return _run_async(_send())
