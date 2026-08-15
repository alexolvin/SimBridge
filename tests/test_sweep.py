"""S03.3 — sweep-recordings.py: orphan forward + bounded retention.

Runs the real script in a subprocess (the same shape as the systemd
unit: an absolute script path, the app root NOT on sys.path) against a
temp config. Artifacts: a capture server standing in for the userbot.

Coverage:
  - age >= sweep_max_retain_seconds  -> deleted WITHOUT forwarding
  - age >= sweep_max_age_seconds, forward fails -> kept for retry
  - age <  sweep_max_age_seconds     -> untouched (still being written)
  - forward succeeds                 -> multipart OggS, file consumed
  - no recordings dir                -> clean no-op (exit 0)
"""

from __future__ import annotations

import http.server
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SWEEP = REPO / "scripts" / "sweep-recordings.py"

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class _Capture:
    """Minimal POST capture — stands in for the userbot /events/voicemail."""

    def __init__(self):
        self.requests: list[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                self.requests.append({  # type: ignore[attr-defined]
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length),
                })
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):  # silence
                pass

        # Handler needs a back-reference to record on the instance
        Handler.requests = self.requests
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def listen(self) -> str:
        return f"127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def capture():
    c = _Capture()
    yield c
    c.stop()


def _make_wav(path: Path, seconds: float = 1.0, rate: int = 8000) -> None:
    """440 Hz tone (same deterministic helper as tests/test_agi.py)."""
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


def _age(path: Path, seconds: int) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


def _write_config(tmp_path: Path, rec_dir: Path, listen: str) -> Path:
    """A minimal valid simbridge.yaml (same shape as tests/conftest.py)."""
    prompt = REPO / "sounds" / "vm-prompt.ulaw"
    content = f"""\
node:
  role: all-in-one
  id: sweep-test

telegram:
  master_username: testuser
  session_path: /tmp/simbridge_sweep_session
  acl_file: /tmp/simbridge_sweep_acl.conf
  api_id_env: SIMBRIDGE_TG_API_ID
  api_hash_env: SIMBRIDGE_TG_API_HASH

agent:
  listen: "127.0.0.1:8090"
  token_env: SIMBRIDGE_AGENT_TOKEN
  userbot_url: http://127.0.0.1:8088
  allowed_peers:
    - "127.0.0.1"

userbot_http:
  listen: "{listen}"
  secret_env: SIMBRIDGE_HTTP_SECRET
  allowed_peers:
    - "127.0.0.1"

asterisk:
  ari_url: http://127.0.0.1:8088/ari
  dongle: gsm
  ring_wait_seconds: 24
  max_record_seconds: 90
  early_hangup_max_seconds: 3
  sweep_max_age_seconds: 300
  sweep_max_retain_seconds: 1000
  prompt: {prompt}
  ami_host: 127.0.0.1
  ami_port: 5038
  ami_username: simbridge
  ami_password_env: SIMBRIDGE_AMI_PASSWORD

voice:
  bridge_endpoint: tg-bridge
  bridge_host: 127.0.0.1
  bridge_port: 5062
  srtp: false
  outbound_answer_timeout: 30

limits:
  sms_per_hour: 30
  calls_per_minute: 3
  max_call_seconds: 3600

paths:
  blacklist: /tmp/simbridge_sweep_blacklist.txt
  contacts_cache: /tmp/simbridge_sweep_contacts.csv
  audit_log: /tmp/simbridge_sweep_audit.jsonl
  sms_correlation: /tmp/simbridge_sweep_sms_correlation.jsonl
  recordings_dir: {rec_dir}
"""
    cfg = tmp_path / "simbridge.yaml"
    cfg.write_text(content)
    return cfg


def _run_sweep(monkeypatch: pytest.MonkeyPatch, cfg_path: Path) -> subprocess.CompletedProcess:
    """Run the script exactly like the systemd unit: absolute path,
    SIMBRIDGE_CONFIG via env, no PYTHONPATH."""
    monkeypatch.setenv("SIMBRIDGE_CONFIG", str(cfg_path))
    for key, val in [
        ("SIMBRIDGE_TG_API_ID", "12345"),
        ("SIMBRIDGE_TG_API_HASH", "0123456789abcdef0123456789abcdef"),
        ("SIMBRIDGE_AGENT_TOKEN", "test-token-1234"),
        ("SIMBRIDGE_HTTP_SECRET", "test-secret-5678"),
        ("SIMBRIDGE_AMI_PASSWORD", "test-ami-pass"),
    ]:
        monkeypatch.setenv(key, val)
    return subprocess.run(
        [sys.executable, str(SWEEP)],
        cwd="/",  # deliberately NOT the repo — the unit's shape
        capture_output=True, text=True, timeout=120,
    )


class TestSweepRetention:
    def test_retention_cap_deletes_without_forwarding(self, tmp_path,
                                                      monkeypatch, capture):
        """S03.3: past the cap a never-forwarded file is dropped —
        and never sent (a stale 7-day-old voicemail is not news)."""
        rec = tmp_path / "recordings"
        rec.mkdir()
        old = rec / "vm-old.wav"
        old.write_bytes(b"RIFF-fake")
        _age(old, 2000)  # >= sweep_max_retain_seconds (1000)

        r = _run_sweep(monkeypatch, _write_config(tmp_path, rec, capture.listen))
        assert r.returncode == 0, r.stderr
        assert not old.exists()
        assert capture.requests == []  # deleted, never forwarded

    def test_failed_forward_keeps_file_for_retry(self, tmp_path, monkeypatch):
        """A failed forward keeps the recording for the next sweep run
        (the AGI path already tried once)."""
        rec = tmp_path / "recordings"
        rec.mkdir()
        mid = rec / "vm-mid.wav"
        mid.write_bytes(b"RIFF-fake")
        _age(mid, 500)  # >= max_age (300), < retain (1000)

        # 127.0.0.1:1 — userbot unreachable, the forward must fail
        r = _run_sweep(monkeypatch, _write_config(tmp_path, rec, "127.0.0.1:1"))
        assert r.returncode == 0, r.stderr
        assert mid.exists()

    def test_young_file_is_untouched(self, tmp_path, monkeypatch):
        """A file younger than sweep_max_age_seconds may still be
        being written — never touch it."""
        rec = tmp_path / "recordings"
        rec.mkdir()
        young = rec / "vm-young.wav"
        young.write_bytes(b"RIFF-fake")
        _age(young, 100)  # < max_age (300)

        r = _run_sweep(monkeypatch, _write_config(tmp_path, rec, "127.0.0.1:1"))
        assert r.returncode == 0, r.stderr
        assert young.exists()

    def test_missing_recordings_dir_is_a_clean_noop(self, tmp_path, monkeypatch):
        r = _run_sweep(monkeypatch,
                       _write_config(tmp_path, tmp_path / "nope", "127.0.0.1:1"))
        assert r.returncode == 0, r.stderr

    @pytest.mark.skipif(not HAVE_FFMPEG,
                        reason="ffmpeg/ffprobe not available")
    def test_successful_forward_deletes_file(self, tmp_path, monkeypatch,
                                             capture):
        """12 s of audio, an 8.444 s greeting -> 3.556 s of speech >=
        3 s -> a normal multipart voice note; the file is consumed."""
        rec = tmp_path / "recordings"
        rec.mkdir()
        wav = rec / "vm-ok.wav"
        _make_wav(wav, seconds=12.0)
        _age(wav, 500)

        r = _run_sweep(monkeypatch, _write_config(tmp_path, rec, capture.listen))
        assert r.returncode == 0, r.stderr
        assert not wav.exists()  # consumed on success
        (req,) = capture.requests
        assert req["path"] == "/events/voicemail"
        assert "multipart/form-data" in req["headers"]["Content-Type"]
        assert b"OggS" in req["body"]  # opus (Ogg container) in the file part
        assert b"unknown" in req["body"]  # channel gone -> caller lost
