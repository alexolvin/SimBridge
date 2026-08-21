#!/usr/bin/env python3
"""Sweep orphaned voicemail recordings and forward them (safety net).

Triggered by the simbridge-sweep systemd timer. The primary forward
path is the dialplan h-exten (tg-voice-agi.py); this sweeper covers
recordings left behind when that path could not run or did not
succeed:

  - Asterisk crashed / was killed between MixMonitor and the h-exten;
  - the h-exten forward failed (userbot down, encode error) and the
    recording was kept for retry;
  - any other orphan (e.g. a file left in /tmp by the old system).

Orphan = a *.wav in the recordings dir older than
asterisk.sweep_max_age_seconds (default 300). Such a file is by
definition no longer being written (max_record_seconds bounds a live
recording), so forwarding it is safe. The caller ID is lost with the
channel, so the event carries "unknown" — an improvement over the old
system, which lost mid-recording hangup recordings entirely.

S03.3 bounded retention: a file older than
asterisk.sweep_max_retain_seconds (default 604800 = 7 days) is DELETED
without forwarding — a failed send must not leave recordings on disk
indefinitely. The greeting length (asterisk.prompt) is probed so the
sweeper classifies and trims exactly like the AGI path.

Runs under the agent venv (PyYAML for config). The forward logic is
core.voicemail_forward (Rule 1 — same implementation as the AGI
path). The secret comes from the process environment (the unit's
EnvironmentFile=/etc/simbridge/env) — never from the YAML.

Usage:
    /opt/simbridge-venv/bin/python /opt/simbridge/scripts/sweep-recordings.py

Exit code 0 on success (including "nothing to sweep"); 1 on config
errors. systemd journal carries the log lines (stderr).
"""

from __future__ import annotations

import os
import sys
import time

# Python puts the SCRIPT's directory on sys.path, not the cwd — add
# the app root explicitly so `core` resolves however the script is
# invoked (the systemd unit, a manual run, a test). Same bootstrap
# pattern as generate_asterisk_config.py (Rule 1: one pattern).
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from core import voicemail_forward as vfm  # noqa: E402
from core.config import ConfigError, load_config, resolve_userbot_url  # noqa: E402

DEFAULT_MAX_AGE_SECONDS = 300
# S03.3: bounded retention for un-forwarded recordings (7 days).
DEFAULT_MAX_RETAIN_SECONDS = 604800
DEFAULT_RECORDINGS_DIR = "/var/lib/simbridge/recordings"
DEFAULT_CALLER = "unknown"


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        vfm.log(f"ERROR: invalid config, cannot sweep: {e}")
        return 1

    rec_dir = cfg.get("paths.recordings_dir", DEFAULT_RECORDINGS_DIR)
    max_age = int(cfg.get("asterisk.sweep_max_age_seconds", DEFAULT_MAX_AGE_SECONDS))
    # S03.3: bounded retention — delete rather than keep-forever.
    max_retain = int(cfg.get(
        "asterisk.sweep_max_retain_seconds", DEFAULT_MAX_RETAIN_SECONDS,
    ))
    eh_max = int(cfg.get(
        "asterisk.early_hangup_max_seconds",
        vfm.DEFAULT_EARLY_HANGUP_MAX_SECONDS,
    ))
    # S03.1: probe the greeting length so classification and trimming
    # match the AGI path exactly (0.0 if the prompt is absent/broken).
    prompt_path = cfg.get("asterisk.prompt", "")
    prompt_duration = vfm.ffprobe_duration(prompt_path) if prompt_path else 0.0
    # Same URL the generator publishes as USERBOT_URL (Rule 1 —
    # core.config.resolve_userbot_url). On a gsm-role node the userbot
    # lives on the PEER telegram node; the local userbot_http.listen
    # is a dead address there (live incident 2026-08-21: every sweep
    # run refused on the node's own IP while the userbot was up).
    url = resolve_userbot_url(cfg)
    secret_env = cfg.get("userbot_http.secret_env", "SIMBRIDGE_HTTP_SECRET")
    secret = os.environ.get(secret_env, "")

    if not os.path.isdir(rec_dir):
        vfm.log(f"No recordings directory {rec_dir}; nothing to sweep")
        return 0

    now = time.time()
    swept = 0
    for name in sorted(os.listdir(rec_dir)):
        if not name.endswith(".wav"):
            continue
        path = os.path.join(rec_dir, name)
        try:
            age = now - os.path.getmtime(path)
        except OSError:
            continue
        if age < max_age:
            continue

        # S03.3: past the retention cap the file is dropped without
        # forwarding — a failed send must not live on disk forever.
        if age >= max_retain:
            vfm.log(f"Sweep: deleting {path} (age={int(age)}s >= "
                    f"retain={max_retain}s, never forwarded)")
            vfm.cleanup_recording(path)
            continue

        correlation = name.rsplit(".wav", 1)[0]
        vfm.log(f"Sweep: forwarding orphan {path} (age={int(age)}s)")
        ok, detail, vm_type = vfm.forward_recording(
            recording_path=path,
            caller=DEFAULT_CALLER,
            correlation=correlation,
            url=url,
            secret=secret,
            early_hangup_max_seconds=eh_max,
            prompt_duration=prompt_duration,
        )
        if ok:
            vfm.cleanup_recording(path)
            vfm.log(f"Sweep: forwarded {path} type={vm_type} ({detail})")
            swept += 1
        else:
            vfm.log(f"Sweep: {path} — will retry on the next run: {detail}")

    vfm.log(f"Sweep done: {swept} forwarded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
