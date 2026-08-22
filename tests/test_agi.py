"""Subprocess tests for the AGI scripts (P0-3).

Drives the scripts exactly the way Asterisk would: an AGI environment
block on stdin, strict request/response for GET VARIABLE / SET
VARIABLE, and asserts on the HTTP requests the scripts send to a local
test server.

Regression coverage:
  - user data (SMS text, caller ID) must survive commas — the old
    design passed them as AGI argv, which is comma-split;
  - user data must reach the network only as JSON/multipart bodies,
    never via a shell;
  - AGI failures must not wedge the dialplan (script always answers);
  - GET VARIABLE answers must follow the REAL Asterisk 18 wire format
    ("200 result=1 (<value>)" / "200 result=0", res/res_agi.c) — the
    driver emulates it exactly. The pre-fix scripts assumed "200
    <value>" and this driver fed them that, so the two were mutually
    consistent; in production an unset variable came back as the
    literal string "result=0" and crashed the script (2026-08-18).
  - AGI commands must be LF-terminated: the daemon strips only the
    trailing \\n of a command line (res/res_agi.c, run_agi — identical
    in unpatched upstream 18.26.4), so a CRLF command leaves \\r on
    the variable name (GET VARIABLE -> result=0, SET VARIABLE creates
    a "NAME\\r" variable). Live-verified on 3p14-aaa, 2026-08-19. The
    driver reads the pipe as raw bytes and strips only \\n, emulating
    this exactly (see TestAgiWireFormat).
"""

from __future__ import annotations

import http.server
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import wave
from pathlib import Path
from urllib.parse import parse_qs

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# ---------------------------------------------------------------------------
# AGI driver — emulates Asterisk's AGI server side
# ---------------------------------------------------------------------------

def run_agi(script: str, argv: list[str], variables: dict[str, str],
            env_block: dict[str, str] | None = None,
            extra_env: dict[str, str] | None = None,
            timeout: float = 60.0) -> tuple[str, dict]:
    """Run an AGI script the way Asterisk would.

    Feeds the environment block, answers GET VARIABLE / SET VARIABLE
    requests, and returns (final_response, info).

    The script's stdout is read as RAW BYTES and only a trailing
    ``\\n`` is stripped — exactly what the real daemon does (res/
    res_agi.c, run_agi: "get rid of trailing newline, if any"). A
    script that sends CRLF-terminated commands therefore arrives with
    a ``\\r`` on the variable name and gets ``result=0`` here, as in
    production (verified live on 3p14-aaa, 2026-08-19). Reading in
    text mode would apply universal-newline translation and hide the
    ``\\r`` — a "kinder" driver that masks the exact production bug.
    """
    env = dict(os.environ)
    env["SIMBRIDGE_HOME"] = str(REPO)
    env.pop("SIMBRIDGE_HTTP_SECRET", None)
    env.pop("SIMBRIDGE_CONFIG", None)
    if extra_env:
        env.update(extra_env)

    proc = subprocess.Popen(
        [sys.executable, str(SCRIPTS / script), *argv],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env,
    )

    # The real daemon's setup_env terminates the env block with CRLF;
    # the scripts rstrip both, so feed it faithfully.
    for key, value in (env_block or {}).items():
        proc.stdin.write(f"{key}: {value}\r\n".encode())
    proc.stdin.write(b"\r\n")
    proc.stdin.flush()

    final = ""
    set_vars: dict[str, str] = {}
    raw_lines: list[str] = []

    def drive() -> None:
        nonlocal final
        for raw in proc.stdout:
            raw_line = raw.decode(errors="replace")
            raw_lines.append(raw_line)
            # Daemon-faithful: strip the trailing \n only.
            line = raw_line.rstrip("\n")
            if line.startswith("GET VARIABLE "):
                name = line.split(" ", 2)[2]
                # Real Asterisk 18 wire format (res/res_agi.c,
                # handle_getvariable): set -> "200 result=1 (<value>)",
                # unset -> "200 result=0", LF-terminated. Emulate it
                # exactly — a driver that is "kinder" than the real
                # server hides parser bugs (see module docstring).
                if name in variables:
                    proc.stdin.write(
                        f"200 result=1 ({variables[name]})\n".encode())
                else:
                    proc.stdin.write(b"200 result=0\n")
                proc.stdin.flush()
            elif line.startswith("SET VARIABLE "):
                key, _, value = line[len("SET VARIABLE "):].partition(" ")
                set_vars[key] = value
                proc.stdin.write(b"200 result=1\n")
                proc.stdin.flush()
            elif line.startswith("200 "):
                final = line
                return

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        proc.kill()
    proc.wait(timeout=5)
    stderr = proc.stderr.read().decode(errors="replace")

    return final, {
        "returncode": proc.returncode,
        "stderr": stderr,
        "set_vars": set_vars,
        "raw_lines": raw_lines,
    }


# ---------------------------------------------------------------------------
# Local HTTP capture server (stands in for the userbot)
# ---------------------------------------------------------------------------

class _CaptureServer:
    def __init__(self, responses: dict[str, bytes] | None = None) -> None:
        """responses: optional path -> raw response body override
        (default: "{}" for every path)."""
        captured: list[dict] = []
        responses = dict(responses or {})

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                captured.append({
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length),
                })
                body = responses.get(self.path, b"{}")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:  # silence
                pass

        self.captured = captured
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def http_server():
    srv = _CaptureServer()
    yield srv
    srv.stop()


def _make_wav(path: Path, seconds: float = 1.0, rate: int = 8000) -> None:
    """Write a 440 Hz tone (deterministic, nonzero audio for loudnorm)."""
    n = int(rate * seconds)
    frames = b"".join(
        struct.pack("<h", int(32767 * 0.1 * math.sin(2 * math.pi * 440 * i / rate)))
        for i in range(n)
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


# ---------------------------------------------------------------------------
# tg-sms-agi.py
# ---------------------------------------------------------------------------

class TestTgSmsAgi:
    def test_sms_with_commas_in_text(self, http_server):
        """Commas in the SMS body must survive — they travel via the
        channel variable, not argv (argv is comma-split)."""
        final, _ = run_agi(
            "tg-sms-agi.py", ["sms"],
            variables={
                "SMS_FROM": "+79000000000",
                "SMS_TEXT": "Hello, world, with commas",
                "FWD_URL": http_server.url,
                "MODEM_ID": "gsm",
            },
            extra_env={"SIMBRIDGE_HTTP_SECRET": "test-secret"},
        )
        assert final.startswith("200 forwarded"), final
        (req,) = http_server.captured
        assert req["path"] == "/events/sms"
        # HTTP headers are case-insensitive; urllib capitalizes names
        # ("X-SimBridge-Secret" arrives as "X-Simbridge-Secret").
        headers = {k.lower(): v for k, v in req["headers"].items()}
        assert headers["x-simbridge-secret"] == "test-secret"
        payload = json.loads(req["body"])
        assert payload == {
            "phone_number": "+79000000000",
            "text": "Hello, world, with commas",
            "modem_id": "gsm",
        }

    def test_report_event(self, http_server):
        """The RAW carrier text goes to the agent's /v1/sms/report with
        a bearer token (D3) — not the marker to the userbot."""
        final, _ = run_agi(
            "tg-sms-agi.py", ["report"],
            variables={"SMS_FROM": "carrier",
                       "SMS_TEXT": "Delivered 2026-08-15 10:00 +79261234555",
                       "MODEM_ID": "gsm"},
            extra_env={"AGENT_URL": http_server.url,
                       "SIMBRIDGE_AGENT_TOKEN": "test-token"},
        )
        assert final.startswith("200 forwarded"), final
        (req,) = http_server.captured
        assert req["path"] == "/v1/sms/report"
        headers = {k.lower(): v for k, v in req["headers"].items()}
        assert headers["authorization"] == "Bearer test-token"
        payload = json.loads(req["body"])
        assert payload == {
            "phone_number": "carrier",
            "text": "Delivered 2026-08-15 10:00 +79261234555",
            "modem_id": "gsm",
        }

    def test_report_empty_text_is_skipped(self, http_server):
        final, _ = run_agi(
            "tg-sms-agi.py", ["report"],
            variables={"SMS_FROM": "carrier", "SMS_TEXT": "",
                       "MODEM_ID": "gsm"},
            extra_env={"AGENT_URL": http_server.url,
                       "SIMBRIDGE_AGENT_TOKEN": "test-token"},
        )
        assert final.startswith("200 skipped"), final

    def test_report_id_delivered(self, http_server):
        """chan_dongle report channel: SMS_TEXT is the sms_id the agent
        sent as the DongleSendSMS Payload header; SMS_REPORT_SUCCESS=1
        must hit /v1/sms/{id}/delivered (correlation by ID, not text)."""
        sms_id = "2a984199016741baa27bfce68344ac8e"
        final, _ = run_agi(
            "tg-sms-agi.py", ["report"],
            variables={"SMS_FROM": "+79267523624",
                       "SMS_TEXT": sms_id,
                       "SMS_REPORT_SUCCESS": "1",
                       "MODEM_ID": "gsm"},
            extra_env={"AGENT_URL": http_server.url,
                       "SIMBRIDGE_AGENT_TOKEN": "test-token"},
        )
        assert final.startswith("200 resolved=delivered"), final
        (req,) = http_server.captured
        assert req["path"] == f"/v1/sms/{sms_id}/delivered"
        headers = {k.lower(): v for k, v in req["headers"].items()}
        assert headers["authorization"] == "Bearer test-token"

    def test_report_id_failed(self, http_server):
        """SMS_REPORT_SUCCESS=0 (carrier failure or modem send error,
        TYPE "i") must hit /v1/sms/{id}/failed."""
        sms_id = "2a984199016741baa27bfce68344ac8e"
        final, _ = run_agi(
            "tg-sms-agi.py", ["report"],
            variables={"SMS_FROM": "+79267523624",
                       "SMS_TEXT": sms_id,
                       "SMS_REPORT_SUCCESS": "0",
                       "MODEM_ID": "gsm"},
            extra_env={"AGENT_URL": http_server.url,
                       "SIMBRIDGE_AGENT_TOKEN": "test-token"},
        )
        assert final.startswith("200 resolved=failed"), final
        (req,) = http_server.captured
        assert req["path"] == f"/v1/sms/{sms_id}/failed"

    def test_report_success_with_free_text_still_uses_content_match(
        self, http_server
    ):
        """SMS_REPORT_SUCCESS set but SMS_TEXT not a 32-hex id (the
        manual CLI fallback case) must fall back to the legacy
        /v1/sms/report content match."""
        final, _ = run_agi(
            "tg-sms-agi.py", ["report"],
            variables={"SMS_FROM": "carrier",
                       "SMS_TEXT": "Delivered 2026-08-22 10:00 +79261234555",
                       "SMS_REPORT_SUCCESS": "1",
                       "MODEM_ID": "gsm"},
            extra_env={"AGENT_URL": http_server.url,
                       "SIMBRIDGE_AGENT_TOKEN": "test-token"},
        )
        assert final.startswith("200 forwarded"), final
        (req,) = http_server.captured
        assert req["path"] == "/v1/sms/report"

    def test_report_id_agent_down_reports_error_not_crash(self, http_server):
        """Agent unreachable: the ID path must log and answer 200 —
        never raise into the dialplan (Rule 4)."""
        final, _ = run_agi(
            "tg-sms-agi.py", ["report"],
            variables={"SMS_FROM": "+79267523624",
                       "SMS_TEXT": "2a984199016741baa27bfce68344ac8e",
                       "SMS_REPORT_SUCCESS": "1",
                       "MODEM_ID": "gsm"},
            # Port 1 — nothing listens.
            extra_env={"AGENT_URL": "http://127.0.0.1:1",
                       "SIMBRIDGE_AGENT_TOKEN": "test-token"},
        )
        assert final.startswith("200 error="), final
        assert http_server.captured == []
        assert http_server.captured == []

    def test_ring_event(self, http_server):
        """Production parity: the old dialplan sent "RING ${CALLER}"."""
        final, _ = run_agi(
            "tg-sms-agi.py", ["ring"],
            variables={"SMS_FROM": "+79000000000",
                       "FWD_URL": http_server.url, "MODEM_ID": "gsm"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 forwarded"), final
        payload = json.loads(http_server.captured[0]["body"])
        assert payload["text"] == "RING +79000000000"

    def test_ussd_event(self, http_server):
        final, _ = run_agi(
            "tg-sms-agi.py", ["ussd"],
            variables={"SMS_FROM": "gsm", "SMS_TEXT": "100 1234 OK",
                       "FWD_URL": http_server.url, "MODEM_ID": "gsm"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 forwarded"), final
        payload = json.loads(http_server.captured[0]["body"])
        assert payload["phone_number"] == "gsm"
        assert payload["text"] == "100 1234 OK"

    def test_empty_sms_is_skipped(self, http_server):
        final, _ = run_agi(
            "tg-sms-agi.py", ["sms"],
            variables={"SMS_FROM": "+79000000000", "SMS_TEXT": "",
                       "FWD_URL": http_server.url, "MODEM_ID": "gsm"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 skipped"), final
        assert http_server.captured == []

    def test_unknown_event_is_error(self):
        final, _ = run_agi("tg-sms-agi.py", ["bogus"], variables={})
        assert final.startswith("200 error=unknown event"), final

    def test_forward_failure_reports_error_not_crash(self):
        # Port 1 — nothing listens; the script must report, not crash.
        final, info = run_agi(
            "tg-sms-agi.py", ["sms"],
            variables={"SMS_FROM": "+79000000000", "SMS_TEXT": "x",
                       "FWD_URL": "http://127.0.0.1:1", "MODEM_ID": "gsm"},
        )
        assert final.startswith("200 error="), final

    def test_unset_fwd_url_is_not_parsed_as_literal(self):
        """FWD_URL unset -> real Asterisk answers "200 result=0". The
        script must treat it as empty (fall back to the default URL),
        not as the literal string "result=0" — the old parser returned
        that string, which became a URL and crashed the script in
        production (ValueError: unknown url type, 2026-08-18)."""
        final, info = run_agi(
            "tg-sms-agi.py", ["ring"],
            variables={"SMS_FROM": "+79000000000", "MODEM_ID": "gsm"},
        )
        # The default URL (127.0.0.1:8088) has nothing listening on the
        # GSM node, so the forward fails — but it must be a clean,
        # reported failure: a final 200 line, exit 0, no traceback.
        assert final.startswith("200 "), final
        assert info["returncode"] == 0
        assert "Traceback" not in info["stderr"], info["stderr"]


# ---------------------------------------------------------------------------
# tg-voice-agi.py
# ---------------------------------------------------------------------------

class TestTgVoiceAgi:
    def test_missing_file_reports_recording_missing(self, http_server, tmp_path):
        final, _ = run_agi(
            "tg-voice-agi.py", [],
            variables={"VMFILE": str(tmp_path / "nope.wav"),
                       "CALLER": "+79000000000",
                       "FWD_URL": http_server.url,
                       "EH_MAX": "3"},
            env_block={"agi_uniqueid": "test-unique-1"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 forwarded type=recording_missing"), final
        (req,) = http_server.captured
        assert req["path"] == "/events/voicemail"
        assert req["headers"]["Content-Type"] == (
            "application/x-www-form-urlencoded")
        payload = {k: v[0] for k, v in
                   parse_qs(req["body"].decode("utf-8")).items()}
        assert payload["voicemail_type"] == "recording_missing"
        assert payload["correlation_id"] == "test-unique-1"
        assert payload["phone_number"] == "+79000000000"

    def test_no_vmfile_skips(self, http_server):
        final, _ = run_agi("tg-voice-agi.py", [],
                           variables={"FWD_URL": http_server.url})
        assert final.startswith("200 skipped"), final

    def test_forward_failure_keeps_file_and_reports(self, tmp_path):
        wav = tmp_path / "vm-keep.wav"
        wav.write_bytes(b"RIFF-fake")
        final, _ = run_agi(
            "tg-voice-agi.py", [],
            variables={"VMFILE": str(wav), "CALLER": "+79000000000",
                       "FWD_URL": "http://127.0.0.1:1", "EH_MAX": "3"},
            env_block={"agi_uniqueid": "test-unique-3"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 error="), final
        assert wav.exists()  # kept for the sweeper's retry

    @pytest.mark.skipif(not HAVE_FFMPEG,
                        reason="ffmpeg/ffprobe not available")
    def test_early_hangup_is_text_only_form(self, http_server, tmp_path):
        """S03.1: 1 s under the threshold is a greeting fragment +
        silence — a text-only form event, never an audio upload.

        Regression 2026-08-21: the event used to be sent as JSON,
        which the userbot's req.form() parses as an EMPTY form —
        delivered as "normal" from "unknown" with no audio."""
        wav = tmp_path / "vm-test.wav"
        _make_wav(wav, seconds=1.0)
        final, _ = run_agi(
            "tg-voice-agi.py", [],
            variables={"VMFILE": str(wav), "CALLER": "+79000000000",
                       "FWD_URL": http_server.url, "EH_MAX": "3"},
            env_block={"agi_uniqueid": "test-unique-2"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 forwarded type=early_hangup"), final
        assert not wav.exists()  # consumed on success
        (req,) = http_server.captured
        assert req["headers"]["Content-Type"] == (
            "application/x-www-form-urlencoded")
        payload = {k: v[0] for k, v in
                   parse_qs(req["body"].decode("utf-8")).items()}
        assert payload["voicemail_type"] == "early_hangup"
        assert payload["phone_number"] == "+79000000000"
        assert payload["correlation_id"] == "test-unique-2"

    @pytest.mark.skipif(not HAVE_FFMPEG,
                        reason="ffmpeg/ffprobe not available")
    def test_normal_wav_trims_greeting_and_sends_audio(self, http_server, tmp_path):
        """S03.1: 12 s total, an 8.444 s greeting -> 3.556 s of speech
        >= 3 s -> a normal voice note with the greeting trimmed off."""
        wav = tmp_path / "vm-test.wav"
        _make_wav(wav, seconds=12.0)
        final, _ = run_agi(
            "tg-voice-agi.py", [],
            variables={"VMFILE": str(wav), "CALLER": "+79000000000",
                       "FWD_URL": http_server.url, "EH_MAX": "3",
                       "VM_PROMPT_DURATION": "8.444"},
            env_block={"agi_uniqueid": "test-unique-4"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 forwarded type=normal"), final
        assert not wav.exists()  # consumed on success
        (req,) = http_server.captured
        assert "multipart/form-data" in req["headers"]["Content-Type"]
        body = req["body"]
        assert b"normal" in body
        assert b"+79000000000" in body
        assert b"test-unique-4" in body
        assert b"OggS" in body  # opus (Ogg container) in the file part


# ---------------------------------------------------------------------------
# tg-blacklist-agi.py
# ---------------------------------------------------------------------------

class TestTgBlacklistAgi:
    def test_blocks_listed_number(self, tmp_path):
        bl = tmp_path / "blacklist.txt"
        bl.write_text("+79111111111\n")
        final, info = run_agi(
            "tg-blacklist-agi.py", [],
            variables={"CALLER": "+79111111111", "BL_PATH": str(bl)},
        )
        assert final.startswith("200 blocked"), final
        assert info["set_vars"] == {"BL_BLOCKED": "1"}

    def test_allows_unknown_number(self, tmp_path):
        bl = tmp_path / "blacklist.txt"
        bl.write_text("+79111111111\n")
        final, info = run_agi(
            "tg-blacklist-agi.py", [],
            variables={"CALLER": "+79222222222", "BL_PATH": str(bl)},
        )
        assert final.startswith("200 ok"), final
        assert info["set_vars"] == {"BL_BLOCKED": "0"}

    def test_missing_file_fails_open(self, tmp_path):
        final, info = run_agi(
            "tg-blacklist-agi.py", [],
            variables={"CALLER": "+79111111111",
                       "BL_PATH": str(tmp_path / "missing.txt")},
        )
        assert final.startswith("200 ok"), final  # fail-open by design
        assert info["set_vars"] == {"BL_BLOCKED": "0"}

    def test_no_caller_fails_open(self):
        final, info = run_agi("tg-blacklist-agi.py", [],
                              variables={"BL_PATH": "/nonexistent"})
        assert final.startswith("200 ok"), final
        assert info["set_vars"] == {"BL_BLOCKED": "0"}


# ---------------------------------------------------------------------------
# notify-agent-agi.py
# ---------------------------------------------------------------------------

class TestNotifyAgentAgi:
    def test_complete_without_call_id_is_skipped(self):
        """CALL_ID unset -> real Asterisk answers "200 result=0". The
        script must skip (no call registered), not POST to the literal
        path "/v1/call/result=0/complete" (old parser bug)."""
        final, info = run_agi(
            "notify-agent-agi.py", ["complete", "ANSWERED"],
            variables={},
            extra_env={"AGENT_URL": "http://127.0.0.1:1"},
        )
        assert final == "200 skipped=no_call_id", final
        assert info["returncode"] == 0

    def test_incoming_sets_call_id_channel_variable(self):
        """The call_id from the agent's JSON is written to the channel
        via SET VARIABLE CALL_ID (the dialplan gates the Dial on it)."""
        srv = _CaptureServer(
            responses={"/v1/call/incoming": b'{"call_id": "call-42"}'}
        )
        try:
            final, info = run_agi(
                "notify-agent-agi.py", ["incoming", "+790000000000"],
                variables={},
                env_block={"agi_channel": "Dongle/gsm-00000001"},
                extra_env={"AGENT_URL": srv.url,
                           "SIMBRIDGE_AGENT_TOKEN": "test-token"},
            )
        finally:
            srv.stop()
        assert final == "200 registered", final
        assert info["set_vars"] == {"CALL_ID": "call-42"}
        (req,) = srv.captured
        assert req["path"] == "/v1/call/incoming"
        payload = json.loads(req["body"])
        assert payload == {"phone_number": "+790000000000",
                           "gsm_channel_id": "Dongle/gsm-00000001"}

    def test_complete_posts_status_for_set_call_id(self):
        """CALL_ID set -> parsed from the real "200 result=1 (value)"
        format and used in the path (the old parser would have POSTed
        to "/v1/call/result=1 (call-42)/complete")."""
        srv = _CaptureServer()
        try:
            final, info = run_agi(
                "notify-agent-agi.py", ["complete", "ANSWERED"],
                variables={"CALL_ID": "call-42"},
                extra_env={"AGENT_URL": srv.url,
                           "SIMBRIDGE_AGENT_TOKEN": "test-token"},
            )
        finally:
            srv.stop()
        assert final == "200 ok=answered", final
        (req,) = srv.captured
        assert req["path"] == "/v1/call/call-42/complete"
        payload = json.loads(req["body"])
        assert payload == {"status": "answered", "dialstatus": "ANSWERED"}


# ---------------------------------------------------------------------------
# Wire format: AGI commands must be LF-terminated
# ---------------------------------------------------------------------------

class TestAgiWireFormat:
    """Asterisk 18.26.4 strips only the trailing ``\\n`` of an AGI
    command line (res/res_agi.c, run_agi — the EPEL build's file is
    byte-identical to unpatched upstream 18.26.4). A CRLF-terminated
    command leaves a ``\\r`` on the payload: GET VARIABLE looks up
    "NAME\\r" (result=0) and SET VARIABLE creates a "NAME\\r" channel
    variable the dialplan can never see. Live-verified on 3p14-aaa,
    2026-08-19 (same probe: EPOCH via LF -> result=1, via CRLF ->
    result=0). The driver emulates the daemon byte-for-byte, so a
    CRLF regression also fails every behavioral test above; these
    tests make the wire format itself the explicit assertion.
    """

    def test_sms_script_sends_lf_only(self, http_server):
        final, info = run_agi(
            "tg-sms-agi.py", ["sms"],
            variables={"SMS_FROM": "+79000000000", "SMS_TEXT": "x",
                        "FWD_URL": http_server.url, "MODEM_ID": "gsm"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 forwarded"), final
        offenders = [r for r in info["raw_lines"] if "\r" in r]
        assert not offenders, f"CRLF in AGI command lines: {offenders}"

    def test_blacklist_script_sends_lf_only(self, tmp_path):
        (tmp_path / "blacklist.txt").write_text("")
        final, info = run_agi(
            "tg-blacklist-agi.py", [],
            variables={"CALLER": "+79000000000",
                       "BL_PATH": str(tmp_path / "blacklist.txt")},
        )
        assert final.startswith("200 ok"), final
        assert info["set_vars"] == {"BL_BLOCKED": "0"}
        offenders = [r for r in info["raw_lines"] if "\r" in r]
        assert not offenders, f"CRLF in AGI command lines: {offenders}"

    def test_notify_script_sends_lf_only(self):
        srv = _CaptureServer(
            responses={"/v1/call/incoming": b'{"call_id": "call-42"}'}
        )
        try:
            final, info = run_agi(
                "notify-agent-agi.py", ["incoming", "+790000000000"],
                variables={},
                env_block={"agi_channel": "Dongle/gsm-00000001"},
                extra_env={"AGENT_URL": srv.url,
                           "SIMBRIDGE_AGENT_TOKEN": "t"},
            )
        finally:
            srv.stop()
        assert final == "200 registered", final
        assert info["set_vars"] == {"CALL_ID": "call-42"}
        offenders = [r for r in info["raw_lines"] if "\r" in r]
        assert not offenders, f"CRLF in AGI command lines: {offenders}"
