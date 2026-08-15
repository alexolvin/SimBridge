#!/usr/bin/env python3
"""AGI script: notify simbridge-agent about call events (S04 flow).

Called from extensions.conf (dialplan) via AGI() when an incoming call
is registered with the agent or when GSM dialing starts/fails. User
data (caller number, channel ID) arrives as AGI arguments (argv) — no
shell is involved (P0-3).

Usage in dialplan:
    AGI(notify-agent-agi.py,incoming,+7XXXXXXXXXX,channel-uniqueid)
    AGI(notify-agent-agi.py,gsm-dialing,+7XXXXXXXXXX,channel-uniqueid)

S04.3: incoming call flow — the GSM channel is NOT answered until the
Telegram user accepts. This script registers the call with the agent
API so the userbot can start ringing Telegram.

Security: the agent URL and auth token come from the process
environment (AGENT_URL, SIMBRIDGE_AGENT_TOKEN — inherited from
/etc/simbridge/env via the Asterisk systemd drop-in). Secrets never
traverse the dialplan — channel variables are visible via AMI/CLI.

Stdlib only — runs under Asterisk's system python3 (no venv
dependency). A failed agent call must not wedge the dialplan: the
script logs to stderr (captured in the Asterisk log) and always
answers 200.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

AGENT_URL_ENV = "AGENT_URL"
AGENT_TOKEN_ENV = "SIMBRIDGE_AGENT_TOKEN"
DEFAULT_URL = "http://127.0.0.1:8090"
TIMEOUT = 5.0


def _log(msg: str) -> None:
    """Log to stderr — Asterisk captures AGI stderr in its own log."""
    print(msg, file=sys.stderr, flush=True)


def _write(line: str) -> None:
    sys.stdout.write(line + "\r\n")
    sys.stdout.flush()


def _respond(status: str) -> None:
    """Send the final AGI response and end the session."""
    _write(f"200 {status}")
    _write("")


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


def post_json(url: str, token: str, path: str, payload: dict) -> tuple[int, dict, str]:
    """POST JSON to *url*+*path*; return (status, body, error detail)."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url.rstrip("/") + path, data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
            return resp.status, data, "ok"
    except urllib.error.HTTPError as e:
        return e.code, {}, f"HTTP {e.code}"
    except Exception as e:  # URLError, timeout, bad JSON — never crash the dialplan
        return 0, {}, str(e)


def main() -> None:
    read_agi_env()

    if len(sys.argv) < 3:
        _respond("error=usage: notify-agent-agi.py <event> <phone> [channel_id]")
        sys.exit(1)

    event = sys.argv[1]  # "incoming", "gsm-dialing", "gsm-busy", "gsm-no-answer"
    phone = sys.argv[2]

    agent_url = os.environ.get(AGENT_URL_ENV, DEFAULT_URL)
    agent_token = os.environ.get(AGENT_TOKEN_ENV, "")

    if event == "incoming":
        status, data, detail = post_json(
            agent_url, agent_token, "/v1/call/incoming",
            {"phone_number": phone},
        )
        if status == 200:
            call_id = str(data.get("call_id", ""))
            if call_id:
                _write(f"SET VARIABLE CALL_ID {call_id}")
                sys.stdin.readline()  # consume the SET VARIABLE response
            _respond("registered")
        else:
            _log(f"ERROR: agent /v1/call/incoming failed: {detail} (status={status})")
            _respond(f"error={detail}")
    elif event in ("gsm-dialing", "gsm-busy", "gsm-no-answer"):
        # Outcome events: the call is already registered with the agent
        # (created by the userbot when the user requested the call).
        _respond(f"ok event={event}")
    else:
        _respond(f"error=unknown event {event!r}")


if __name__ == "__main__":
    main()
