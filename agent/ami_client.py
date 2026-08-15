"""Asterisk Manager Interface (AMI) client.

Provides parameterized access to Asterisk commands via the chan_dongle
AMI actions (DongleSendSMS, DongleShowDevices) and core AMI actions
(Originate, Hangup, SetVariable, Command). Chosen over ARI because:
1. AMI is available on Asterisk 18 out of the box (no extra config)
2. DongleSendSMS / DongleShowDevices are AMI-level actions, not exposed via ARI REST
3. Simpler dependency: one TCP connection, no HTTP server on Asterisk side

Security: parameters are sent as first-class AMI header fields
("Message: <text>"), never interpolated into a command string. AMI is a
line-based protocol, so values containing raw newlines are rejected with
ValueError instead of corrupting the stream.
"""

from __future__ import annotations

import asyncio
import io
import re
from typing import Optional


class AMISendError(Exception):
    """AMI action failed with an explicit Error response."""

    def __init__(self, message: str, response: dict) -> None:
        super().__init__(message)
        self.response = response


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
        """Open AMI TCP connection and log in.

        AMI protocol: server sends greeting line, then waits for Login.
        Do NOT wait for a blank line after greeting — some Asterisk versions
        (chan_dongle configurations) close idle connections before sending it.
        Send Login immediately after reading the greeting to avoid timeout.
        """
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port
        )

        # Read greeting line (e.g. "Asterisk Call Manager/7.0.3")
        greeting = await self._reader.readline()
        if not greeting:
            raise ConnectionError("AMI server closed connection immediately")

        # Send login immediately — do NOT wait for blank line
        await self._send_action({
            "Action": "Login",
            "UserName": self._username,
            "Secret": self._password,
        })
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

    # chan_dongle GSMRegistrationStatus values that mean the modem is usable.
    _REGISTERED_STATES = {"Registered, home network", "Registered, roaming"}

    async def send_sms(self, to: str, text: str) -> dict:
        """Send SMS via the native DongleSendSMS AMI action.

        *to* and *text* are first-class AMI header fields (Number /
        Message) — never interpolated into a command string, so
        apostrophes, commas and Unicode pass through intact.

        chan_dongle has no multi-line support over AMI, so newlines are
        flattened to spaces before sending (the legacy dialplan never
        supported multi-line SMS either).

        Validity (1440 minutes) and Report (yes) mirror the production
        dialplan's DongleSendSMS application arguments; the delivery
        report drives the /v1/sms/report correlation path.

        Raises:
            AMISendError: the action returned an explicit Error response.
        """
        flattened = re.sub(r"[\r\n]+", " ", text)
        action_id = f"sms-{id(self)}-{asyncio.get_event_loop().time()}"
        await self._send_action(
            {
                "Action": "DongleSendSMS",
                "Device": self._dongle,
                "Number": to,
                "Message": flattened,
                "Validity": "1440",
                "Report": "yes",
                "ActionID": action_id,
            }
        )
        resp = await self._read_response()
        if resp.get("Response") == "Error" or resp.get("Action") == "failure":
            raise AMISendError(resp.get("Message", "DongleSendSMS failed"), resp)
        return resp

    async def get_modem_status(self) -> dict:
        """Query live modem state via the DongleShowDevices AMI action.

        Returns a normalized dict with the keys the callers expect:
        device, registered, signal_percent, operator, imei_suffix —
        mapped from the DongleDeviceEntry fields chan_dongle emits
        (GSMRegistrationStatus, RSSI, ProviderName, IMEIState).
        Returns {} if the device is not found.
        """
        action_id = f"status-{id(self)}-{asyncio.get_event_loop().time()}"
        await self._send_action(
            {
                "Action": "DongleShowDevices",
                "Device": self._dongle,
                "ActionID": action_id,
            }
        )
        # Reply shape: a list-ack message, then one DongleDeviceEntry
        # per matching device, then a list-complete message. Unrelated
        # events can interleave — skip anything that is not the entry.
        entry: dict[str, str] = {}
        for _ in range(32):  # bounded: a device list is short (1-3 entries)
            resp = await self._read_response()
            if resp.get("Message") == "DongleDeviceEntry":
                entry = resp
                break
            if resp.get("Message") == "ListComplete":
                break
        if not entry:
            return {}
        return self._normalize_device_entry(entry)

    @classmethod
    def _normalize_device_entry(cls, entry: dict[str, str]) -> dict:
        """Map raw DongleDeviceEntry fields to the normalized status dict."""
        registered = entry.get("GSMRegistrationStatus", "") in cls._REGISTERED_STATES

        signal = None
        # chan_dongle emits "RSSI: %d, %s" — the first part is the dBm int.
        rssi = entry.get("RSSI", "")
        try:
            dbm = int(rssi.split(",")[0].strip())
            # Map -110..-50 dBm onto 0..100 %
            signal = max(0, min(100, round((dbm + 110) * 100 / 60)))
        except (ValueError, IndexError):
            signal = None

        imei = entry.get("IMEIState", "")
        return {
            "device": entry.get("Device", ""),
            "registered": registered,
            "signal_percent": signal,
            "operator": entry.get("ProviderName") or None,
            "imei_suffix": imei[-4:] if imei else None,
        }

    # -------------------------------------------------------------------
    # S04.3: Call control AMI actions
    # -------------------------------------------------------------------

    async def originate_call(
        self,
        endpoint: str,
        context: str = "default",
        extension: str = "s",
        priority: int = 1,
        caller_id: Optional[str] = None,
        channel_vars: Optional[dict[str, str]] = None,
    ) -> dict:
        """Initiate an outbound call via AMI Originate.

        Used to answer the GSM leg for incoming calls, or dial GSM for outgoing calls.
        """
        action_id = f"orig-{id(self)}-{asyncio.get_event_loop().time()}"
        action: dict[str, str] = {
            "Action": "Originate",
            "Channel": endpoint,
            "Context": context,
            "Extension": extension,
            "Priority": str(priority),
            "ActionID": action_id,
        }
        if caller_id:
            action["CallerID"] = caller_id
        if channel_vars:
            # Build Variable headers — AMI uses Variable: KEY=VALUE (one per line)
            for k, v in channel_vars.items():
                # AMI allows multiple Variable headers — use the last one
                action["Variable"] = f"{k}={v}"

        await self._send_action(action)
        return await self._read_response()

    async def hangup_channel(
        self,
        channel_id: str,
        reason: str = "BYE",
    ) -> dict:
        """Hang up a specific Asterisk channel via AMI.

        Used for symmetric hangup — terminating the other leg when one leg ends.
        """
        action_id = f"hang-{id(self)}-{asyncio.get_event_loop().time()}"
        await self._send_action(
            {
                "Action": "Hangup",
                "Channel": channel_id,
                "Reason": reason,
                "ActionID": action_id,
            }
        )
        return await self._read_response()

    async def answer_channel(self, channel_id: str) -> dict:
        """Answer a ringing channel via AMI.

        Used to answer the GSM leg when the Telegram user accepts.
        """
        action_id = f"answ-{id(self)}-{asyncio.get_event_loop().time()}"
        await self._send_action(
            {
                "Action": "Redirect",
                "Channel": channel_id,
                "Context": "default",
                "Extension": "s",
                "Priority": "1",
                "ActionID": action_id,
            }
        )
        # Use Originate with wait=0 to answer a ringing channel, or use the
        # simpler approach: originate to Local/s@answer
        # Actually: use a direct AMI action — Answer — but it doesn't exist
        # The standard approach is to use Channel(url) or Originate(Local/..)
        # For chan_dongle, the channel answers when Dial() is issued.
        # We use Originate to a Local channel that answers the existing channel.
        return await self._send_answer(channel_id)

    async def _send_answer(self, channel_id: str) -> dict:
        """Answer a channel by originating to it with Answer action.

        AMI 'Answer' action: answers a ringing channel.
        """
        action_id = f"ansr-{id(self)}-{asyncio.get_event_loop().time()}"
        await self._send_action(
            {
                "Action": "Command",
                "Command": f"channel {channel_id} answer",
                "ActionID": action_id,
            }
        )
        return await self._read_response()

    async def list_channels(self) -> list[dict]:
        """List all active Asterisk channels via AMI CoreShowChannels.

        Used for orphan channel detection after cleanup.
        """
        action_id = f"list-{id(self)}-{asyncio.get_event_loop().time()}"
        await self._send_action(
            {
                "Action": "CoreShowChannels",
                "ActionID": action_id,
            }
        )
        return [await self._read_response()]

    async def set_channel_variable(
        self,
        channel_id: str,
        variable: str,
        value: str,
    ) -> dict:
        """Set a channel variable via AMI.

        Used to signal state changes to the dialplan (e.g., TG_ACCEPTED=1).
        """
        action_id = f"var-{id(self)}-{asyncio.get_event_loop().time()}"
        await self._send_action(
            {
                "Action": "SetVariable",
                "Channel": channel_id,
                "Variable": variable,
                "Value": value,
                "ActionID": action_id,
            }
        )
        return await self._read_response()

    async def _send_action(self, fields: dict) -> None:
        """Send an AMI action message.

        AMI is a line-based protocol: a raw newline inside a value would
        terminate the field early and corrupt the stream, so such values
        are rejected instead of emitting an invalid message.
        """
        if not self._writer:
            raise ConnectionError("AMI client not connected")

        for k, v in fields.items():
            if "\r" in v or "\n" in v:
                raise ValueError(
                    f"AMI field {k!r} contains a newline — AMI is line-based"
                )

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
