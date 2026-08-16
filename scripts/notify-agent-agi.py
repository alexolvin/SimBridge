#!/usr/bin/env python3
"""AGI script: notify simbridge-agent about call events (Stage 04).

Called from extensions.conf via AGI() at three points:

    AGI(notify-agent-agi.py,incoming,${CALLER})
    AGI(notify-agent-agi.py,outgoing-accepted)
    AGI(notify-agent-agi.py,complete,${DIALSTATUS})   ; or ,complete,ENDED in dead mode

Events:
  incoming           register an incoming call with the agent
                     (POST /v1/call/incoming); on success sets the
                     CALL_ID channel variable for the later complete
                     event.
  outgoing-accepted  the Telegram user accepted an outgoing call and
                     the bridge INVITEd this node
                     (POST /v1/call/outgoing/accepted); on success
                     sets CALL_ID, which gates the GSM dial. A 404
                     (call already expired by the TG ring timeout)
                     leaves CALL_ID unset -> the dialplan skips the
                     GSM dial.
  complete           the call leg ended. The argument is either a raw
                     DIALSTATUS (live s-exten — Dial blocked for the
                     whole call, so the status is final) or the
                     literal ENDED (dead-mode h-exten, where
                     DIALSTATUS is stale). Reports
                     POST /v1/call/<CALL_ID>/complete; a 404 there
                     (already terminal — expected double-POST from
                     s-exten + h-exten) is a no-op.

The agent URL and auth token come from the process environment
(AGENT_URL, SIMBRIDGE_AGENT_TOKEN — inherited from /etc/simbridge/env
via the Asterisk systemd drop-in). Secrets never traverse the dialplan
— channel variables are visible via AMI/CLI. User data (caller number)
arrives as AGI arguments (argv) — no shell is involved (P0-3). The
channel identity (ASTERAISK_CHANNEL from the AGI environment) is passed
in the JSON payloads so the agent can AMI-hangup the right channel when
enforcing the call duration limit.

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

# Dial() returns with a final DIALSTATUS for the whole call (it blocks
# until the call ends, not on answer). The dead-mode h-exten passes the
# literal ENDED instead, because DIALSTATUS is stale there.
DIALSTATUS_TO_STATUS = {
    "ANSWERED": "answered",
    "NOANSWER": "no_answer",
    "BUSY": "busy",
    "CANCEL": "cancelled",
    "": "ended",
    "ENDED": "ended",
}


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


def agi_get_variable(name: str) -> str:
    """GET VARIABLE <name>; return the value ("" if unset).

    Dead-safe: res/res_agi.c serves GET VARIABLE on hungup channels.
    """
    _write(f"GET VARIABLE {name}")
    line = sys.stdin.readline().strip()
    if line.startswith("200"):
        return line[3:].strip()
    _log(f"WARNING: GET VARIABLE {name} -> {line!r}")
    return ""


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
    env = read_agi_env()

    if len(sys.argv) < 2:
        _respond("error=usage: notify-agent-agi.py <event> [arg]")
        sys.exit(1)

    event = sys.argv[1]  # "incoming", "outgoing-accepted", "complete"

    agent_url = os.environ.get(AGENT_URL_ENV, DEFAULT_URL)
    agent_token = os.environ.get(AGENT_TOKEN_ENV, "")
    channel = env.get("ASTERAISK_CHANNEL", "")

    if event == "incoming":
        if len(sys.argv) < 3:
            _respond("error=usage: notify-agent-agi.py incoming <phone>")
            sys.exit(1)
        status, data, detail = post_json(
            agent_url, agent_token, "/v1/call/incoming",
            {"phone_number": sys.argv[2], "gsm_channel_id": channel},
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

    elif event == "outgoing-accepted":
        status, data, detail = post_json(
            agent_url, agent_token, "/v1/call/outgoing/accepted",
            {"bridge_channel_id": channel},
        )
        if status == 200:
            call_id = str(data.get("call_id", ""))
            if call_id:
                _write(f"SET VARIABLE CALL_ID {call_id}")
                sys.stdin.readline()
            _respond("accepted")
        elif status == 404:
            # The call already expired (TG ring timeout with a late
            # accept) — leave CALL_ID unset so the dialplan skips the
            # GSM dial (nocal gate).
            _log("outgoing call not found (expired?) — not dialing")
            _respond("skipped=not_found")
        else:
            _log(f"ERROR: agent /v1/call/outgoing/accepted failed: {detail} (status={status})")
            _respond(f"error={detail}")

    elif event == "complete":
        raw = sys.argv[2] if len(sys.argv) > 2 else ""
        status_name = DIALSTATUS_TO_STATUS.get(raw, "failed")
        call_id = agi_get_variable("CALL_ID")
        if not call_id:
            _respond("skipped=no_call_id")
            return
        status, data, detail = post_json(
            agent_url, agent_token,
            f"/v1/call/{call_id}/complete",
            {"status": status_name, "dialstatus": raw},
        )
        if status in (200, 404):
            # 404 = already terminal: the call was closed by another
            # event first (expected double-POST, e.g. s-exten and
            # h-exten both reporting). Not an error.
            _respond(f"ok={status_name}")
        else:
            _log(f"ERROR: agent /v1/call/{call_id}/complete failed: {detail} (status={status})")
            _respond(f"error={detail}")

    else:
        _respond(f"error=unknown event {event!r}")


if __name__ == "__main__":
    main()
