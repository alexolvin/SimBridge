#!/usr/bin/env python3
"""AGI script: check an incoming caller against the blacklist (call path).

Replaces the old dialplan construction

    Set(BL_COUNT=${SHELL(grep -c ... /etc/asterisk/blacklist/numbers.txt)})

which passed the caller ID (user-controlled, spoofable from the GSM
network) into a shell (P0-3, RCE).

Here no user data reaches a shell: the caller number and the blacklist
path arrive as channel variables (SET by the dialplan, read with
GET VARIABLE), and the file is read inside the interpreter.

Usage in dialplan::

    exten => s,1,Set(CALLER=${CALLERID(num)})
     same => n,Set(BL_PATH=${BLACKLIST_PATH})
     same => n,AGI(tg-blacklist-agi.py)
     same => n,GotoIf($["${BL_BLOCKED}" = "1"]?blacklisted)

On completion the script sets the channel variable BL_BLOCKED (1 or 0)
and always answers 200 — the check must never break the call path.

Fail-open on error: the old behavior when grep could not read the file
was "0 matches" (call proceeds). A broken check must not drop a
legitimate call; the error is logged to stderr (captured in the
Asterisk log).

Imports core.blacklist from the deployed app tree so there is exactly
one blacklist mechanism and one E.164 normalizer (Rule 1). The tree
root comes from the SIMBRIDGE_HOME environment variable (written to
/etc/simbridge/env by install.py; fallback default below).

Stdlib only — runs under Asterisk's system python3.
"""

from __future__ import annotations

import os
import sys

DEFAULT_HOME = "/opt/simbridge"


def _log(msg: str) -> None:
    """Log to stderr — Asterisk captures AGI stderr in its own log."""
    print(msg, file=sys.stderr, flush=True)


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

    The response is one ``200 <value>`` line. AGI is strictly
    request/response, so a single readline is the complete response.
    """
    sys.stdout.write(f"GET VARIABLE {name}\r\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    line = line.rstrip("\r\n")
    if line.startswith("200 "):
        return line[4:]
    return ""


def is_blacklisted(caller: str, path: str) -> bool:
    """True if *caller* is listed in the blacklist file at *path*."""
    home = os.environ.get("SIMBRIDGE_HOME", DEFAULT_HOME)
    if home not in sys.path:
        sys.path.insert(0, home)
    # Rule 1: one blacklist mechanism, one E.164 normalizer
    from core.blacklist import BlacklistManager

    return BlacklistManager(path).contains(caller)


def main() -> None:
    read_agi_env()

    caller = agi_get_variable("CALLER")
    path = agi_get_variable("BL_PATH")

    blocked = False
    if not caller:
        _log("WARNING: blacklist check called without CALLER — failing open")
    elif not path:
        _log("WARNING: BL_PATH not set — failing open")
    else:
        try:
            blocked = is_blacklisted(caller, path)
        except Exception as e:  # noqa: BLE001 — fail-open by design
            _log(f"ERROR: blacklist check failed, failing open: {e}")

    sys.stdout.write(f"SET VARIABLE BL_BLOCKED {'1' if blocked else '0'}\r\n")
    sys.stdout.flush()
    sys.stdin.readline()  # consume the SET VARIABLE response (AGI is
                          # strictly request/response)
    sys.stdout.write(f"200 {'blocked' if blocked else 'ok'}\r\n\r\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
