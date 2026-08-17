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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

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


async def _make_server(with_entry=True, entry=ENTRY, legacy_headers=False):
    """Fake AMI server: greeting, Login ack, then DongleShowDevices reply.

    legacy_headers=True emits the entry/terminator under Message:
    headers (the format the old code assumed) to pin backwards
    compatibility.
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
                    writer.write(ACK.format(aid=aid).encode())
                    if with_entry:
                        writer.write(entry)
                    writer.write(complete)
                await writer.drain()
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
