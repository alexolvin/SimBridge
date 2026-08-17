"""Concurrency tests for the AMI client.

Regression test for a production race: connect() left _reader/_writer
assigned after a failed login, and a concurrent poller/watchdog command
raced a second readline() on the same StreamReader, raising
"readuntil() called while another coroutine is already waiting for
incoming data" and wedging every later AMI transaction.

The fix under test: every transaction (connect, command, close) is
serialized on one lock, a failed connect drops the streams, and reads
are bounded by AMI_READ_TIMEOUT so a wedged socket cannot hold the lock
forever.

No pytest-asyncio in this environment: each test runs its scenario in a
fresh event loop via _run() (same pattern as test_modem_pool.py).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import ami_client  # noqa: E402
from ami_client import AMIClient  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _make_server(fail_login=False, delay=0.0, answer=True):
    """Fake AMI server: greeting, then a request/response loop.

    fail_login — reply Error to the Login action; delay — sleep before
    answering Login (widens the connect/command race window); answer —
    when False the server reads actions but never replies (a wedged
    socket for the read-timeout test).
    """
    async def handle(reader, writer):
        # Greeting line only — the deployed Asterisk (chan_dongle build)
        # sends no trailing blank line after the greeting; the client's
        # connect() documents and relies on that (a leftover blank line
        # would be consumed as an empty Login response).
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
                if delay and "Action: Login" in action:
                    await asyncio.sleep(delay)
                if not answer:
                    continue
                if "Action: Login" in action and fail_login:
                    writer.write(
                        b"Response: Error\r\n"
                        b"Response-Text: Authentication failed\r\n\r\n"
                    )
                else:
                    writer.write(
                        b"Response: Success\r\n"
                        b"Response-Text: Done\r\n\r\n"
                    )
                await writer.drain()
        finally:
            try:
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


class TestFailedLoginDropsStreams:
    def test_failed_login_drops_streams(self):
        async def scenario():
            server, port = await _make_server(fail_login=True)
            try:
                c = AMIClient(port=port)
                with pytest.raises(ConnectionError, match="login failed"):
                    await c.connect()
                # The half-open socket must be gone — a concurrent
                # coroutine must not be able to readline() on it.
                assert c._reader is None and c._writer is None
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())

    def test_command_after_failed_login_is_clean_error(self):
        async def scenario():
            server, port = await _make_server(fail_login=True)
            try:
                c = AMIClient(port=port)
                with pytest.raises(ConnectionError, match="login failed"):
                    await c.connect()
                with pytest.raises(ConnectionError, match="not connected"):
                    await c.send_sms("+15550001111", "hello")
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())


class TestConcurrentConnectAndCommand:
    def test_race_fails_cleanly_not_readuntil(self):
        async def scenario():
            server, port = await _make_server(fail_login=True, delay=0.3)
            try:
                c = AMIClient(port=port)
                conn = asyncio.create_task(c.connect())
                await asyncio.sleep(0.1)
                # Pre-fix: this second readline() on the same
                # StreamReader raised RuntimeError("readuntil() called
                # while another coroutine is already waiting").
                # Post-fix: the command waits on the lock, then sees the
                # dropped streams and fails with a clean ConnectionError.
                with pytest.raises(ConnectionError, match="not connected"):
                    await c.send_sms("+15550001111", "hello")
                with pytest.raises(ConnectionError, match="login failed"):
                    await conn
                assert c._reader is None and c._writer is None
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())

    def test_success_serializes_behind_connect(self):
        async def scenario():
            server, port = await _make_server(fail_login=False, delay=0.3)
            try:
                c = AMIClient(port=port)
                conn = asyncio.create_task(c.connect())
                await asyncio.sleep(0.1)
                # The command must block on the connect lock until the
                # login succeeds — then work on the authenticated stream.
                resp = await c.send_sms("+15550001111", "hello")
                await conn  # no exception
                assert resp.get("Response") == "Success"
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())


class TestReadTimeout:
    def test_wedged_socket_drops_streams_and_releases_lock(
            self, monkeypatch):
        monkeypatch.setattr(ami_client, "AMI_READ_TIMEOUT", 0.3)

        async def scenario():
            server, port = await _make_server(answer=False)
            try:
                c = AMIClient(port=port)
                # Server never answers the Login: the read must time out,
                # drop the streams and release the transaction lock
                # (a wedged read holding the lock would stall the
                # poller, watchdog and reconnector process-wide).
                with pytest.raises(ConnectionError, match="timed out"):
                    await c.connect()
                assert c._reader is None and c._writer is None
            finally:
                server.close()
                await server.wait_closed()

        _run(scenario())
