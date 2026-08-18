#!/usr/bin/env python3
"""AGI script: forward a voicemail recording to the userbot (P0-3).

Called from the ``h`` extension of the [incoming-mobile] context.
Asterisk 18 runs the h-exten after the channel hangs up; AGI() on a
hungup channel is explicitly supported ("dead mode") and GET VARIABLE
is a dead-safe AGI command — both verified in the Asterisk 18 source
(res/res_agi.c). StopMixMonitor() in the h-exten finalizes the WAV
synchronously before this script runs (app_mixmonitor.c: "closing the
filestream here guarantees the file is available to the dialplan after
calling StopMixMonitor").

No user data reaches a shell (P0-3):
  - the recording path, caller ID, and forward URL arrive as channel
    variables (SET by the dialplan, read with GET VARIABLE);
  - the correlation ID is the AGI environment's agi_uniqueid
    (Asterisk 18 env keys are lowercase agi_* — res/res_agi.c,
    setup_env, 18.26.4 — not the uppercase names in older AGI docs);
  - the HTTP secret comes from the process environment
    (SIMBRIDGE_HTTP_SECRET, inherited from Asterisk's EnvironmentFile).

The forward logic lives in core/voicemail_forward.py (Rule 1 — one
implementation, shared with the sweep-recordings.py timer).

Behavior (see core/voicemail_forward.forward_recording):
  1. no recording file       -> JSON event voicemail_type=recording_missing
  2. zero audio (0 s)        -> JSON event voicemail_type=early_hangup
  3. speech < EH_MAX         -> JSON (text only) voicemail_type=early_hangup
  4. else                    -> voicemail_type=normal (multipart, greeting
  trimmed off per VM_PROMPT_DURATION — S03.1)
  5. on success: the recording is deleted (consumed — the h-exten STAT
     check and the sweep timer must not resend it); on failure: kept
     so the sweep timer (or a retry) picks it up.

"speech" = recording duration minus the greeting (S03.1: MixMonitor
starts before Playback, so the greeting is captured at the front of the
WAV).

Usage in dialplan::

    exten => h,1,NoOp(Hangup handler)
     same => n,StopMixMonitor()
     same => n,GotoIf($["${VMFILE}" = ""]?end)
     same => n,GotoIf($[${STAT(e,${VMFILE})} = 0]?end)
     same => n,AGI(tg-voice-agi.py)
     same => n(end),Hangup()

The s-exten sets FWD_URL, MODEM_ID, EH_MAX, VM_PROMPT_DURATION and
CALLER (channel variables persist into the h-exten on the same
channel).

Stdlib only — runs under Asterisk's system python3 (the shared module
core.voicemail_forward is stdlib-only as well). A failure must never
wedge the dialplan: the script logs to stderr (Asterisk log) and
always answers 200.
"""

from __future__ import annotations

import os
import sys
import traceback

SECRET_ENV = "SIMBRIDGE_HTTP_SECRET"
DEFAULT_URL = "http://127.0.0.1:8088"
DEFAULT_EARLY_HANGUP_MAX_SECONDS = 3
DEFAULT_HOME = "/opt/simbridge"


def _log(msg: str) -> None:
    """Log to stderr — Asterisk captures AGI stderr in its own log."""
    print(msg, file=sys.stderr, flush=True)


def _respond(status: str) -> None:
    """Send the final AGI response and end the session.

    LF-terminated — see agi_get_variable for the daemon's line-parse
    behavior.
    """
    sys.stdout.write(f"200 {status}\n\n")
    sys.stdout.flush()


def read_agi_env() -> dict[str, str]:
    """Read the initial AGI environment block (terminated by a blank line)."""
    env: dict[str, str] = {}
    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line:
            break
        if ": " in line:
            key, _, value = line.partition(": ")
            env[key] = value
    return env


def agi_get_variable(name: str) -> str:
    """GET VARIABLE *name*; return the value ("" if unset).

    Asterisk 18 responds ``200 result=1 (<value>)`` for a set
    variable and ``200 result=0`` for an unset one (res/res_agi.c,
    handle_getvariable: literal parens, LF-terminated, value capped
    at 1023 chars). AGI is strictly request/response, so a single
    readline is the complete response. Dead-safe in Asterisk 18
    (command table: "get variable" is marked for dead/hungup
    channels).

    The command itself MUST be LF-terminated: the daemon strips only
    the trailing ``\\n`` of a command line (res/res_agi.c, run_agi;
    identical in unpatched upstream 18.26.4), so a CRLF command
    leaves a ``\\r`` on the variable name and the exact-name lookup
    fails (result=0). Live-verified on 3p14-aaa, probe 2026-08-19.
    """
    sys.stdout.write(f"GET VARIABLE {name}\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    line = line.rstrip("\r\n")
    prefix = "200 result=1 ("
    if line.startswith(prefix) and line.endswith(")"):
        return line[len(prefix):-1]
    return ""


def load_forward_module():
    """Import core.voicemail_forward from the deployed app tree.

    Rule 1: one forward implementation, shared with the sweeper.
    SIMBRIDGE_HOME points at the app root (written to /etc/simbridge/env
    by install.py; fallback default below).
    """
    home = os.environ.get("SIMBRIDGE_HOME", DEFAULT_HOME)
    if home not in sys.path:
        sys.path.insert(0, home)
    from core import voicemail_forward

    return voicemail_forward


def main() -> None:
    env = read_agi_env()

    vmfile = agi_get_variable("VMFILE")
    caller = agi_get_variable("CALLER") or "unknown"
    url = agi_get_variable("FWD_URL") or DEFAULT_URL
    try:
        eh_max = int(agi_get_variable("EH_MAX") or DEFAULT_EARLY_HANGUP_MAX_SECONDS)
    except ValueError:
        eh_max = DEFAULT_EARLY_HANGUP_MAX_SECONDS
    # S03.1: greeting length (the generated PROMPT_DURATION global).
    # Missing/invalid -> 0.0 = legacy classification, no trim.
    try:
        prompt_duration = float(agi_get_variable("VM_PROMPT_DURATION") or 0)
    except ValueError:
        prompt_duration = 0.0
    correlation = env.get("agi_uniqueid", "")
    secret = os.environ.get(SECRET_ENV, "")

    if not vmfile:
        _log("WARNING: AGI called without VMFILE — nothing to forward")
        _respond("skipped=no recording")
        return

    try:
        vfm = load_forward_module()
    except Exception as e:  # noqa: BLE001 — never wedge the dialplan
        _log(f"ERROR: cannot load voicemail_forward module: {e}")
        _respond("error=module")
        return

    try:
        ok, detail, vm_type = vfm.forward_recording(
            recording_path=vmfile,
            caller=caller,
            correlation=correlation,
            url=url,
            secret=secret,
            early_hangup_max_seconds=eh_max,
            prompt_duration=prompt_duration,
        )
    except Exception:  # noqa: BLE001 — never wedge the dialplan
        _log("ERROR: voicemail forward crashed:\n" + traceback.format_exc())
        _respond("error=internal")
        return

    if ok:
        # Consumed: delete so the STAT check and the sweep timer
        # do not resend the same recording.
        vfm.cleanup_recording(vmfile)
        _log(f"Forwarded voicemail from {caller}: type={vm_type} ({detail})")
        _respond(f"forwarded type={vm_type}")
    else:
        _log(f"ERROR: voicemail forward failed for {caller}: {detail} "
             f"(recording kept for retry)")
        _respond(f"error={detail}")


if __name__ == "__main__":
    main()
