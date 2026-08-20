"""Wire-shape tests for DongleShowDevices against the real chan_dongle format.

Regression for a production bug: the client matched the device entry and
list terminator on ``Message:`` headers, but the deployed chan_dongle
build emits them as ``Event:`` headers (verified from the compiled
module: ``strings chan_dongle.so`` shows ``Event: DongleDeviceEntry``
and ``Event: DongleShowDevicesComplete``). The mismatch meant every
poll read past the end of the list until the 15 s AMI read timeout
dropped the stream — modem poller failing, the watchdog resetting via
full AMI reconnect every ~45 s, and /v1/health (which runs two more
get_modem_status() calls on the same serialized stream) hanging well
past any client curl timeout, which failed the installer's
"Agent health endpoint" verify on every deploy.

The fake server below speaks the exact wire format of the deployed
module (greeting line only — the chan_dongle build sends no trailing
blank line after the greeting, same as test_ami_client_race.py).

No pytest-asyncio in this environment: each test runs its scenario in
a fresh event loop via _run().
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import ami_client  # noqa: E402
from ami_client import AMIClient  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Real wire format of the deployed chan_dongle (strings chan_dongle.so):
#   ack:              Response: Success / Message: Device status list will follow
#   entry:            Event: DongleDeviceEntry + Device/GSMRegistrationStatus/
#                     RSSI/ProviderName/IMEIState fields
#   terminator:       Event: DongleShowDevicesComplete
ACK = (
    "Response: Success\r\n"
    "Action: DongleShowDevices\r\n"
    "Message: Device status list will follow\r\n"
    "ActionID: {aid}\r\n"
    "\r\n"
)
ENTRY = (
    b"Event: DongleDeviceEntry\r\n"
    b"Device: gsm\r\n"
    b"GSMRegistrationStatus: Registered, home network\r\n"
    b"RSSI: -65, -65\r\n"
    b"ProviderName: T2\r\n"
    b"IMEIState: 123456789012345\r\n"
    b"\r\n"
)
COMPLETE = b"Event: DongleShowDevicesComplete\r\n\r\n"

# A call-lifecycle event with no ActionID — what a live call floods
# into the AMI stream (one 17 s call measured 64+ of these,
# 2026-08-20 3p14-aaa).
BURST_EVENT = (
    b"Event: Newstate\r\n"
    b"Channel: Dongle/gsm-00000001\r\n"
    b"State: 6\r\n"
    b"CallerIDNum: +79267523624\r\n"
    b"\r\n"
)


async def _make_server(
    with_entry=True,
    entry=ENTRY,
    legacy_headers=False,
    lead_events=0,
    mid_events=0,
    stale_complete=False,
    complete_actionid=False,
):
    """Fake AMI server: greeting, Login ack, then DongleShowDevices reply.

    legacy_headers=True emits the entry/terminator under Message:
    headers (the format the old code assumed) to pin backwards
    compatibility.

    Burst knobs (regression for the 32-cap exhaustion):
    lead_events — call events written BEFORE the list-ack (stale
    backlog from an in-flight call); mid_events — events written
    BETWEEN the entry and the terminator; stale_complete — one bare
    leftover terminator written first (what an interrupted old-code
    drain leaves in the stream); complete_actionid — the terminator
    carries our ActionID (the deployed chan_dongle shape).
    """
    if legacy_headers:
        entry = entry.replace(b"Event: ", b"Message: ")
        complete = b"Message: ListComplete\r\n\r\n"
    else:
        complete = COMPLETE

    async def handle(reader, writer):
        writer.write(b"Asterisk Call Manager/7.0.3\r\n")
        await writer.drain()
        try:
            while True:
                action = ""
                while True:
                    line = await reader.readline()
                    if not line:
                        return
                    if line in (b"\r\n", b"\n"):
                        break
                    action += line.decode()
                if not action.strip():
                    continue
                if "Action: Login" in action:
                    writer.write(
                        b"Response: Success\r\n"
                        b"Response-Text: Done\r\n"
                        b"\r\n"
                    )
                elif "DongleShowDevices" in action:
                    aid = ""
                    for l in action.splitlines():
                        if l.startswith("ActionID:"):
                            aid = l.split(":", 1)[1].strip()
                    if stale_complete:
                        writer.write(COMPLETE)
                    for _ in range(lead_events):
                        writer.write(BURST_EVENT)
                    writer.write(ACK.format(aid=aid).encode())
                    if with_entry:
                        writer.write(entry)
                    for _ in range(mid_events):
                        writer.write(BURST_EVENT)
                    if complete_actionid:
                        writer.write(
                            b"Event: DongleShowDevicesComplete\r\n"
                            b"ActionID: " + aid.encode() + b"\r\n"
                            b"EventList: Complete\r\n"
                            b"ListItems: 1\r\n"
                            b"\r\n"
                        )
                    else:
                        writer.write(complete)
                await writer.drain()
        except (ConnectionError, OSError):
            # client went away mid-reply (e.g. drain cap hit)
            return
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server


class TestRealWireShape:
    def test_dongle_show_devices_event_shape(self):
        async def scenario():
            server = await _make_server()
            try:
                c = AMIClient(port=server.sockets[0].getsockname()[1])
                await c.connect()
                try:
                    status = await c.get_modem_status()
                finally:
                    await c.close()
                # Pre-fix: the Message:-only markers never matched, the
                # loop read to the 15 s timeout and raised
                # ConnectionError. Post-fix: the Event: entry is
                # recognized and normalized.
                assert status == {
                    "device": "gsm",
                    "registered": True,
                    "signal_percent": 75,  # (-65+110)*100/60
                    "operator": "T2",
                    "imei_suffix": "2345",
                }
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())

    def test_second_call_not_poisoned_by_leftover_complete(self):
        """The reply must be drained to its terminator.

        If the first call stopped at the entry, the leftover
        Complete message would sit in the stream and the next call
        would read it first — reporting an empty device list for a
        plugged-in dongle.
        """
        async def scenario():
            server = await _make_server()
            try:
                c = AMIClient(port=server.sockets[0].getsockname()[1])
                await c.connect()
                try:
                    first = await c.get_modem_status()
                    second = await c.get_modem_status()
                finally:
                    await c.close()
                assert first["registered"] is True
                assert second == first
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())

    def test_empty_device_list(self):
        async def scenario():
            server = await _make_server(with_entry=False)
            try:
                c = AMIClient(port=server.sockets[0].getsockname()[1])
                await c.connect()
                try:
                    status = await c.get_modem_status()
                finally:
                    await c.close()
                assert status == {}
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())

    def test_unregistered_state_on_wire(self):
        entry = (
            b"Event: DongleDeviceEntry\r\n"
            b"Device: gsm\r\n"
            b"GSMRegistrationStatus: Not registered, not searching\r\n"
            b"RSSI: -110, -110\r\n"
            b"ProviderName: T2\r\n"
            b"IMEIState: 123456789012345\r\n"
            b"\r\n"
        )
        async def scenario():
            server = await _make_server(entry=entry)
            try:
                c = AMIClient(port=server.sockets[0].getsockname()[1])
                await c.connect()
                try:
                    status = await c.get_modem_status()
                finally:
                    await c.close()
                # The real module's unregistered value string
                # (strings chan_dongle.so) must map to registered=False.
                assert status["registered"] is False
                assert status["device"] == "gsm"
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())


class TestLegacyMessageHeaders:
    def test_message_header_variant_still_parses(self):
        """Builds that emit the markers under Message: keep working."""
        async def scenario():
            server = await _make_server(legacy_headers=True)
            try:
                c = AMIClient(port=server.sockets[0].getsockname()[1])
                await c.connect()
                try:
                    status = await c.get_modem_status()
                finally:
                    await c.close()
                assert status["registered"] is True
                assert status["operator"] == "T2"
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())


class TestMarkerHelpers:
    def test_ack_is_neither_entry_nor_complete(self):
        ack = {
            "Response": "Success",
            "Action": "DongleShowDevices",
            "Message": "Device status list will follow",
            "ActionID": "status-1",
        }
        assert AMIClient._is_device_entry(ack) is False
        assert AMIClient._is_list_complete(ack) is False

    def test_entry_without_header_matches_on_fields(self):
        # Field-based fallback: a future build dropping the header
        # marker is still recognized by entry-only fields.
        entry = {
            "Device": "gsm",
            "GSMRegistrationStatus": "Registered, home network",
        }
        assert AMIClient._is_device_entry(entry) is True
        assert AMIClient._is_list_complete(entry) is False

    def test_complete_variants(self):
        assert AMIClient._is_list_complete(
            {"Event": "DongleShowDevicesComplete"}) is True
        assert AMIClient._is_list_complete(
            {"Message": "ListComplete"}) is True
        assert AMIClient._is_list_complete(
            {"Event": "SomeOtherEvent"}) is False


class TestCallEventBurstBacklog:
    """Regression: a live call's AMI event burst must not break the poll.

    2026-08-20 (3p14-aaa): get_modem_status() drained a fixed 32
    messages. One 17-second call floods 64+ lifecycle events
    (Newstate/VarSet/DialBegin/DialEnd/AGI/RTCP/Hangup) into the same
    AMI stream, so 2-3 consecutive 30-second polls exhausted the cap
    on the burst and left their own reply unread: the poller marked a
    healthy dongle "device not present" (dongle_offline alert) and the
    next call attempt got a spurious 503. Every spurious poll stopped
    at exactly 32 messages.
    """

    def test_burst_backlog_before_reply(self):
        """70 stale events (more than the old 32 cap) precede the reply.

        Pre-fix: the cap stopped at 32, mid-burst — the poll's own
        reply was never read and the dongle was reported absent.
        """
        async def scenario():
            server = await _make_server(lead_events=70)
            try:
                c = AMIClient(port=server.sockets[0].getsockname()[1])
                await c.connect()
                try:
                    status = await c.get_modem_status()
                finally:
                    await c.close()
                assert status["registered"] is True
                assert status["device"] == "gsm"
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())

    def test_burst_between_entry_and_complete(self):
        """Events interleave inside the list, up to the terminator."""
        async def scenario():
            server = await _make_server(mid_events=40)
            try:
                c = AMIClient(port=server.sockets[0].getsockname()[1])
                await c.connect()
                try:
                    status = await c.get_modem_status()
                finally:
                    await c.close()
                assert status["registered"] is True
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())

    def test_full_call_burst(self):
        """The measured shape: 32 pre-burst + 40 mid, ActionID-stamped
        terminator (the deployed chan_dongle format) — one real call
        is 64-96 events end to end."""
        async def scenario():
            server = await _make_server(
                lead_events=32, mid_events=40, complete_actionid=True
            )
            try:
                c = AMIClient(port=server.sockets[0].getsockname()[1])
                await c.connect()
                try:
                    status = await c.get_modem_status()
                finally:
                    await c.close()
                assert status["registered"] is True
                assert status["signal_percent"] == 75
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())

    def test_stale_terminator_before_ack_is_skipped(self):
        """A leftover Complete from an interrupted old-code drain sits
        in the stream before our list-ack. It must be skipped, not
        mistaken for our terminator (which would report the dongle
        absent while the real reply follows)."""
        async def scenario():
            server = await _make_server(stale_complete=True)
            try:
                c = AMIClient(port=server.sockets[0].getsockname()[1])
                await c.connect()
                try:
                    status = await c.get_modem_status()
                finally:
                    await c.close()
                assert status["registered"] is True
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())

    def test_drain_cap_backstop(self, monkeypatch, caplog):
        """A stream with no terminator must hit the cap, not hang the
        poller: bounded drain + WARNING, empty result (the per-read
        15 s timeout is the other backstop)."""
        monkeypatch.setattr(ami_client, "AMI_DRAIN_CAP", 5)
        async def scenario():
            server = await _make_server(lead_events=10)
            try:
                c = AMIClient(port=server.sockets[0].getsockname()[1])
                await c.connect()
                try:
                    status = await c.get_modem_status()
                finally:
                    await c.close()
                assert status == {}
            finally:
                server.close()
                await server.wait_closed()

        with caplog.at_level(logging.WARNING, logger="simbridge.ami"):
            _run(scenario())
        assert "drain cap" in caplog.text
