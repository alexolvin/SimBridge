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

Concurrency: the client is a single request/response stream, so every
transaction (connect, command, close) is serialized on one lock, and a
failed login drops the streams — a half-open, unauthenticated socket can
never be shared by two coroutines awaiting readline() on the same
StreamReader (that race raised "readuntil() called while another
coroutine is already waiting for incoming data" in production).
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import Optional

# A hung AMI socket must not hold the transaction lock forever: the
# poller, the watchdog and the reconnector all funnel through it, so
# one wedged read would stall every AMI operation in the process.
AMI_READ_TIMEOUT = 15.0

# The DongleShowDevices reply is drained to its own terminator; this
# cap is the backstop for a run-away stream (a device list is 1-3
# entries). It must fit a full call's AMI event burst (64+ interleaved
# Newstate/VarSet/DialBegin/AGI/RTCP messages) with room to spare: the
# old 32-message cap was exhausted by the burst and left the poll's
# own reply unread, which the poller misread as "dongle absent" —
# spurious offline flap + 503 on the next call attempt (2026-08-20,
# 3p14-aaa: every spurious-offline poll stopped at exactly 32).
AMI_DRAIN_CAP = 1024

logger = logging.getLogger("simbridge.ami")


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
        # AMI is one request/response stream: every transaction must be
        # atomic, and a reconnect must not swap the streams mid-transaction
        # (see module docstring for the production race this prevents).
        # Created lazily in _get_lock(): on Python 3.9 asyncio.Lock()
        # binds to the current event loop at construction, so building it
        # here would attach a client constructed outside a running loop
        # (send_sms_sync, tests) to the wrong one.
        self._lock: Optional[asyncio.Lock] = None

    async def connect(self) -> None:
        """Open AMI TCP connection and log in.

        AMI protocol: server sends greeting line, then waits for Login.
        Do NOT wait for a blank line after greeting — some Asterisk versions
        (chan_dongle configurations) close idle connections before sending it.
        Send Login immediately after reading the greeting to avoid timeout.

        On any failure the streams are dropped, so a failed login never
        leaves a live, unauthenticated socket that another coroutine
        (poller, watchdog) could race us on.
        """
        async with self._get_lock():
            self._reader, self._writer = await asyncio.open_connection(
                self._host, self._port
            )
            try:
                # Read greeting line (e.g. "Asterisk Call Manager/7.0.3")
                try:
                    greeting = await asyncio.wait_for(
                        self._reader.readline(), timeout=AMI_READ_TIMEOUT
                    )
                except asyncio.TimeoutError as e:
                    raise ConnectionError(
                        f"AMI greeting timed out after {AMI_READ_TIMEOUT:.0f}s"
                    ) from e
                if not greeting:
                    raise ConnectionError(
                        "AMI server closed connection immediately"
                    )

                # Send login immediately — do NOT wait for blank line
                await self._send_action({
                    "Action": "Login",
                    "UserName": self._username,
                    "Secret": self._password,
                })
                resp = await self._read_response()
                if resp.get("Response") not in ("Success", "Followed"):
                    raise ConnectionError(f"AMI login failed: {resp}")
            except BaseException:
                # Drop the streams on every failure path — including
                # cancellation — before re-raising.
                self._drop()
                raise

    async def close(self) -> None:
        async with self._get_lock():
            writer = self._writer
            self._reader = None
            self._writer = None
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    def _drop(self) -> None:
        """Release the streams without waiting (no lock — callers either
        hold it or are unwinding a failed transaction)."""
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()

    def _get_lock(self) -> asyncio.Lock:
        """The transaction lock, created on first use inside the running
        loop (see the _lock comment in __init__). Creation is a single
        synchronous step, so two coroutines on the same loop can never
        observe different locks."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # chan_dongle GSMRegistrationStatus values that mean the modem is usable
    # (verified against the deployed module: `strings chan_dongle.so` lists
    # exactly "Registered, home network" / "Registered, roaming" as the
    # usable states, plus "Not registered, [but searching|not searching]"
    # and "Registration denied").
    _REGISTERED_STATES = {"Registered, home network", "Registered, roaming"}

    # chan_dongle's reply to DongleShowDevices, verified against the
    # compiled module on the deployed Asterisk build (`strings
    # chan_dongle.so`): an ack ("Device status list will follow"), then
    # "Event: DongleDeviceEntry" per device, then
    # "Event: DongleShowDevicesComplete". The markers live in the Event:
    # header, NOT Message: — the earlier Message:-only checks never
    # matched, so every poll read past the list until the 15 s read
    # timeout dropped the stream (poller failure → watchdog reset storm
    # → /v1/health hangs, which failed the installer's health verify).
    # Some builds emit the same headers as Message: — accept both.
    _ENTRY_MARKER = "DongleDeviceEntry"
    _COMPLETE_MARKERS = ("DongleShowDevicesComplete", "ListComplete")

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
        async with self._get_lock():
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
        async with self._get_lock():
            await self._send_action(
                {
                    "Action": "DongleShowDevices",
                    "Device": self._dongle,
                    "ActionID": action_id,
                }
            )
            # Reply shape: a list-ack, then one DongleDeviceEntry per
            # matching device, then a list-complete. Unrelated call
            # events (Newstate/VarSet/DialBegin/AGI/RTCP/...) interleave
            # freely — one call floods 64+ of them into the stream.
            #
            # The drain runs to OUR terminator, not to a fixed message
            # count: the old 32-cap was exhausted by a call's event
            # burst and left this reply unread, so the poller marked a
            # healthy, ringing dongle "not present" — spurious offline
            # flap + dongle_offline alert + 503 on the next attempt
            # (2026-08-20, 3p14-aaa: every spurious poll stopped at
            # exactly 32 messages).
            #
            # Attribution: the list-ack (Asterisk core) and the
            # entry/complete (chan_dongle, manager.c) all carry this
            # ActionID. A stale leftover from an interrupted drain
            # (old-code 32-cap hits, or a prior 1024-cap hit) always
            # PRECEDES our list-ack — the stream is FIFO — so any entry
            # or complete seen before the ack is not ours and is
            # skipped; anything seen after it is.
            entry: dict[str, str] = {}
            acked = False
            terminated = False
            for _ in range(AMI_DRAIN_CAP):
                resp = await self._read_response()
                aid = resp.get("ActionID") == action_id
                if self._is_device_entry(resp) and (aid or acked):
                    if not entry:
                        entry = resp
                elif self._is_list_complete(resp):
                    if aid or acked:
                        terminated = True
                        break
                    # stale terminator (pre-ack) — skip, keep draining
                elif resp.get("Response") in ("Success", "Followed") and aid:
                    acked = True
            else:
                logger.warning(
                    "get_modem_status(%s): drain cap %d hit without "
                    "list terminator; action_id=%s",
                    self._dongle, AMI_DRAIN_CAP, action_id,
                )
            if not entry:
                logger.warning(
                    "get_modem_status(%s): no device entry; action_id=%s; "
                    "terminated=%s",
                    self._dongle, action_id, terminated,
                )
                return {}
            return self._normalize_device_entry(entry)

    @classmethod
    def _is_device_entry(cls, resp: dict) -> bool:
        """True for a DongleDeviceEntry message.

        Matches the Event:/Message: header marker; the field-based
        fallback (GSMRegistrationStatus appears only on entries) guards
        against a future build renaming the header.
        """
        hdr = resp.get("Event", "") or resp.get("Message", "")
        if cls._ENTRY_MARKER in hdr:
            return True
        return "GSMRegistrationStatus" in resp and "Device" in resp

    @classmethod
    def _is_list_complete(cls, resp: dict) -> bool:
        """True for the DongleShowDevices list-terminator message."""
        hdr = resp.get("Event", "") or resp.get("Message", "")
        return any(m in hdr for m in cls._COMPLETE_MARKERS)

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

        async with self._get_lock():
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
        async with self._get_lock():
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
        """Answer a ringing channel.

        Deliberately unsupported via AMI: there is no "Answer" AMI action
        and the Asterisk 18 CLI has no ``channel <chan> answer`` verb
        (only originate/redirect/request-hangup — verified via
        ``core show help``). In the shipped architecture the GSM channel
        is answered by the dialplan: ``Dial(SIP/${BRIDGE_ENDPOINT})``
        auto-answers it when the bridge's UAS answers the INVITE. ARI's
        ``POST /channels/{id}/answer`` would be the AMI-side equivalent,
        but ARI is deliberately not enabled (see module docstring).

        Raising is deliberate, not an error path: the legacy
        implementation sent two actions (Redirect + Command) but read
        only one response, which desynchronizes the single
        request/response AMI stream for every subsequent transaction.

        Raises:
            NotImplementedError: always — AMI cannot answer a channel.
        """
        raise NotImplementedError(
            "AMI cannot answer a channel (no AMI action, no CLI verb); "
            f"the dialplan Dial() answers {channel_id} when the bridge "
            "endpoint connects — ARI is required for AMI-side answer"
        )

    async def list_channels(self) -> list[dict]:
        """List all active Asterisk channels via AMI CoreShowChannels.

        Used for orphan channel detection after cleanup.
        """
        action_id = f"list-{id(self)}-{asyncio.get_event_loop().time()}"
        async with self._get_lock():
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
        async with self._get_lock():
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
        """Read one AMI response message.

        Bounded by AMI_READ_TIMEOUT: a wedged socket must not hold the
        transaction lock forever, and a silent drop of the streams turns
        the stall into a clean reconnect cycle.
        """
        if not self._reader:
            raise ConnectionError("AMI client not connected")

        headers: dict[str, str] = {}
        while True:
            try:
                line = await asyncio.wait_for(
                    self._reader.readline(), timeout=AMI_READ_TIMEOUT
                )
            except asyncio.TimeoutError as e:
                self._drop()
                raise ConnectionError(
                    f"AMI read timed out after {AMI_READ_TIMEOUT:.0f}s"
                ) from e
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
