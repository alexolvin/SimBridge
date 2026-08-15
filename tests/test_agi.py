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
  - AGI failures must not wedge the dialplan (script always answers).
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
        env=env, text=True,
    )

    for key, value in (env_block or {}).items():
        proc.stdin.write(f"{key}: {value}\r\n")
    proc.stdin.write("\r\n")
    proc.stdin.flush()

    final = ""
    set_vars: dict[str, str] = {}

    def drive() -> None:
        nonlocal final
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line.startswith("GET VARIABLE "):
                name = line.split(" ", 2)[2]
                proc.stdin.write(f"200 {variables.get(name, '')}\r\n")
                proc.stdin.flush()
            elif line.startswith("SET VARIABLE "):
                key, _, value = line[len("SET VARIABLE "):].partition(" ")
                set_vars[key] = value
                proc.stdin.write("200 result=1\r\n")
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
    stderr = proc.stderr.read()

    return final, {
        "returncode": proc.returncode,
        "stderr": stderr,
        "set_vars": set_vars,
    }


# ---------------------------------------------------------------------------
# Local HTTP capture server (stands in for the userbot)
# ---------------------------------------------------------------------------

class _CaptureServer:
    def __init__(self) -> None:
        captured: list[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                captured.append({
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length),
                })
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

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
            env_block={"UNIQUEID": "test-unique-1"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 forwarded type=recording_missing"), final
        (req,) = http_server.captured
        assert req["path"] == "/events/voicemail"
        payload = json.loads(req["body"])
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
            env_block={"UNIQUEID": "test-unique-3"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 error="), final
        assert wav.exists()  # kept for the sweeper's retry

    @pytest.mark.skipif(not HAVE_FFMPEG,
                        reason="ffmpeg/ffprobe not available")
    def test_early_hangup_is_text_only_json(self, http_server, tmp_path):
        """S03.1: 1 s under the threshold is a greeting fragment +
        silence — a text-only JSON event, never an audio upload."""
        wav = tmp_path / "vm-test.wav"
        _make_wav(wav, seconds=1.0)
        final, _ = run_agi(
            "tg-voice-agi.py", [],
            variables={"VMFILE": str(wav), "CALLER": "+79000000000",
                       "FWD_URL": http_server.url, "EH_MAX": "3"},
            env_block={"UNIQUEID": "test-unique-2"},
            extra_env={"SIMBRIDGE_HTTP_SECRET": "s"},
        )
        assert final.startswith("200 forwarded type=early_hangup"), final
        assert not wav.exists()  # consumed on success
        (req,) = http_server.captured
        assert req["headers"]["Content-Type"] == "application/json"
        payload = json.loads(req["body"])
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
            env_block={"UNIQUEID": "test-unique-4"},
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
