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

from core import voicemail_forward as vfm
from core.config import ConfigError, load_config

DEFAULT_MAX_AGE_SECONDS = 300
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
    eh_max = int(cfg.get(
        "asterisk.early_hangup_max_seconds",
        vfm.DEFAULT_EARLY_HANGUP_MAX_SECONDS,
    ))
    # Same key the generator uses for USERBOT_URL — on the GSM node
    # this is the userbot node's address (two-node topology).
    url = "http://" + cfg["userbot_http.listen"]
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

        correlation = name.rsplit(".wav", 1)[0]
        vfm.log(f"Sweep: forwarding orphan {path} (age={int(age)}s)")
        ok, detail, vm_type = vfm.forward_recording(
            recording_path=path,
            caller=DEFAULT_CALLER,
            correlation=correlation,
            url=url,
            secret=secret,
            early_hangup_max_seconds=eh_max,
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
