#!/usr/bin/env python3
"""AGI script: forward incoming SMS and delivery reports to the userbot.

Replaces the old dialplan line

    System(/usr/local/bin/tg-sms-forward.sh "${DONGLE_FROM}" "${DONGLE_TEXT}")

which interpolated user-controlled SMS text into a shell command — an
injection surface reachable by anyone who can send an SMS to the number
(P0-3, RCE). In this script user data reaches the network only as a
JSON body:

  - the event type (``sms``/``report``/``ring``/``ussd``) is the only
    AGI argument (argv — no shell);
  - the sender number and the text are channel variables (SET by the
    dialplan, read with GET VARIABLE). They cannot go in argv: AGI
    splits arguments on commas, and a comma in an SMS body would
    truncate it;
  - the forward URL (FWD_URL) and modem ID (MODEM_ID) are channel
    variables set from the generated globals (USERBOT_URL, MODEM_ID);
  - the HTTP secret comes from the process environment
    (``SIMBRIDGE_HTTP_SECRET``), inherited from Asterisk's systemd
    ``EnvironmentFile``. Secrets never traverse the dialplan — channel
    variables are visible via AMI/CLI.

Delivery reports (event ``report``) go a different way: the raw
carrier text is POSTed to the **agent** (not the userbot) at
``{AGENT_URL}/v1/sms/report`` with ``Authorization: Bearer
{SIMBRIDGE_AGENT_TOKEN}`` — both from the process environment. The
agent matches the report against its correlation store and resolves
the record as delivered/failed.

Usage in dialplan::

    exten => sms,1,Set(FWD_URL=${USERBOT_URL})
     same => n,Set(MODEM_ID=${MODEM_ID})
     same => n,Set(SMS_FROM=${CALLERID(num)})
     same => n,Set(SMS_TEXT=${BASE64_DECODE(${SMS_BASE64})})
     same => n,AGI(tg-sms-agi.py,sms)
     same => n,Hangup()

    exten => report,1,Set(SMS_FROM=${CALLERID(num)})
     same => n,Set(SMS_TEXT=${BASE64_DECODE(${SMS_BASE64})})
     same => n,Set(MODEM_ID=${MODEM_ID})
     same => n,AGI(tg-sms-agi.py,report)
     same => n,Hangup()

    ; ring notification for an incoming voice call (production parity:
    ; the old dialplan sent "RING ${CALLER}" through the same script)
    exten => s,1,...Set(SMS_FROM=${CALLER})...
     same => n,AGI(tg-sms-agi.py,ring)

    exten => ussd,1,Set(SMS_FROM=${DONGLENAME})
     same => n,Set(SMS_TEXT=${BASE64_DECODE(${USSD_BASE64})})
     same => n,AGI(tg-sms-agi.py,ussd)

Stdlib only — runs under Asterisk's system python3 (no venv
dependency). On failure the script logs to stderr (captured in the
Asterisk log) and continues the dialplan — an SMS forwarding outage
must not wedge the channel.

Known limitation: AGI GET VARIABLE returns a single line. GSM SMS
bodies do not contain CRLF, so real texts arrive intact; a hypothetical
CR/LF inside the text would truncate it (data loss, not an injection).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

SECRET_ENV = "SIMBRIDGE_HTTP_SECRET"
AGENT_TOKEN_ENV = "SIMBRIDGE_AGENT_TOKEN"
DEFAULT_URL = "http://127.0.0.1:8088"
DEFAULT_AGENT_URL = "http://127.0.0.1:8090"
DEFAULT_MODEM_ID = "gsm"
TIMEOUT = 5.0


def _log(msg: str) -> None:
    """Log to stderr — Asterisk captures AGI stderr in its own log."""
    print(msg, file=sys.stderr, flush=True)


def _respond(status: str) -> None:
    """Send the final AGI response and end the session."""
    sys.stdout.write(f"200 {status}\r\n\r\n")
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

    The response is one ``200 <value>`` line. AGI is strictly
    request/response — no other line can arrive before our next
    command, so a single readline is the complete response.
    """
    sys.stdout.write(f"GET VARIABLE {name}\r\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    line = line.rstrip("\r\n")
    if line.startswith("200 "):
        return line[4:]
    return ""


def post_json(url: str, headers: dict, path: str, payload: dict) -> tuple[bool, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=body,
        headers={
            "Content-Type": "application/json",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except Exception as e:  # URLError, HTTPError, timeout — never crash the dialplan
        return False, str(e)


def main() -> None:
    read_agi_env()

    if len(sys.argv) < 2:
        _respond("error=usage: tg-sms-agi.py <sms|report>")
        sys.exit(1)

    event = sys.argv[1]
    if event not in ("sms", "report", "ring", "ussd"):
        _respond(f"error=unknown event {event!r}")
        sys.exit(1)

    sender = agi_get_variable("SMS_FROM")
    modem_id = agi_get_variable("MODEM_ID") or DEFAULT_MODEM_ID

    if event == "report":
        # Delivery report: the raw carrier text goes to the AGENT
        # (AGENT_URL + bearer token from the process environment),
        # which matches it against its correlation store.
        text = agi_get_variable("SMS_TEXT")
        if not text:
            _log("report: empty SMS_TEXT — nothing to correlate, skipping")
            _respond("skipped=empty text")
            return
        url = os.environ.get("AGENT_URL") or DEFAULT_AGENT_URL
        token = os.environ.get(AGENT_TOKEN_ENV, "")
        payload = {"phone_number": sender, "text": text, "modem_id": modem_id}
        ok, detail = post_json(
            url, {"Authorization": f"Bearer {token}"}, "/v1/sms/report", payload
        )
        if ok:
            _log(f"Forwarded delivery report from {sender} (len={len(text)}) to {url}")
            _respond("forwarded")
        else:
            _log(
                f"ERROR: failed to forward delivery report "
                f"from {sender} to {url}: {detail}"
            )
            _respond(f"error={detail}")
        return

    if event == "ring":
        # Production parity: the old dialplan sent "RING ${CALLER}".
        text = f"RING {sender}" if sender else "RING"
    else:  # sms, ussd
        text = agi_get_variable("SMS_TEXT")
    url = agi_get_variable("FWD_URL") or DEFAULT_URL
    secret = os.environ.get(SECRET_ENV, "")

    if not text:
        # Empty SMS/USSD — nothing to forward (matches the old
        # script's behavior: the shell script sent an empty string).
        _respond("skipped=empty text")
        return

    payload = {"phone_number": sender, "text": text, "modem_id": modem_id}
    ok, detail = post_json(url, {"X-SimBridge-Secret": secret}, "/events/sms", payload)

    if ok:
        _log(f"Forwarded {event} from {sender} (len={len(text)}) to {url}")
        _respond("forwarded")
    else:
        _log(f"ERROR: failed to forward {event} from {sender} to {url}: {detail}")
        _respond(f"error={detail}")


if __name__ == "__main__":
    main()
