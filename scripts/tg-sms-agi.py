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

Delivery reports (event ``report``) go a different way, to the
**agent** (not the userbot), with ``Authorization: Bearer
{SIMBRIDGE_AGENT_TOKEN}`` — both from the process environment:

  - chan_dongle fires a report channel per delivery report whose
    SMS_TEXT is the sms_id the agent sent in the DongleSendSMS
    Payload header; with SMS_REPORT_SUCCESS ("1"/"0") the AGI POSTs
    ``/v1/sms/{sms_id}/delivered`` or ``/failed`` — correlation by ID;
  - a free-text SMS_TEXT (not a 32-hex id — only reachable via the
    manual CLI fallback in the dialplan) still goes to
    ``/v1/sms/report`` and is matched by content against the
    correlation store. A carrier text report that arrives as an
    ordinary incoming SMS takes the [sms] exten instead and is
    forwarded to the userbot as a regular SMS.

Usage in dialplan::

    exten => sms,1,Set(FWD_URL=${USERBOT_URL})
     same => n,Set(MODEM_ID=${MODEM_ID})
     same => n,Set(SMS_FROM=${CALLERID(num)})
     same => n,Set(SMS_TEXT=${BASE64_DECODE(${SMS_BASE64})})
     same => n,AGI(tg-sms-agi.py,sms)
     same => n,Hangup()

    ; SMS_REPORT_PAYLOAD (the agent's sms_id) first, SMS_BASE64
    ; (legacy text report) as fallback — see asterisk/extensions.conf
    exten => report,1,Set(SMS_FROM=${CALLERID(num)})
     same => n,Set(SMS_TEXT=${SMS_REPORT_PAYLOAD})
     same => n,GotoIf($["${SMS_TEXT}" != ""]?text_set)
     same => n,Set(SMS_TEXT=${BASE64_DECODE(${SMS_BASE64})})
     same => n(text_set),Set(MODEM_ID=${MODEM_ID})
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
import re
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
    at 1023 chars). AGI is strictly request/response — no other
    line can arrive before our next command, so a single readline
    is the complete response.

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
        # Delivery report — two shapes reach this exten (the dialplan
        # sets SMS_TEXT from whichever one fired):
        #   1. chan_dongle report channel (its own Local channel per
        #      delivery report): SMS_TEXT = SMS_REPORT_PAYLOAD, which
        #      the agent set to the sms_id at send time, plus
        #      SMS_REPORT_SUCCESS "1"/"0". Correlate by ID — reliable,
        #      no dependence on the carrier's report text (inter-
        #      carrier routes often deliver no text report at all).
        #   2. Free text (manual CLI fallback: the exten dialed with
        #      SMS_BASE64 set) — correlate by content via
        #      /v1/sms/report.
        text = agi_get_variable("SMS_TEXT")
        if not text:
            # No sms_id payload and no text — nothing to correlate.
            # Still log the carrier's success flag: reports that carry
            # neither (sends made before the Payload-header feature, or
            # inter-carrier routes that drop the payload) are otherwise
            # invisible. Live gap 2026-08-22 03:16 MSK: two delayed
            # carrier reports for stale +79267523624 sends arrived with
            # empty payloads and their SUCCESS verdict was lost.
            success = agi_get_variable("SMS_REPORT_SUCCESS")
            _log(
                f"report: empty SMS_TEXT (SMS_REPORT_SUCCESS="
                f"{success or 'unset'}) — nothing to correlate, skipping"
            )
            _respond("skipped=empty text")
            return
        url = os.environ.get("AGENT_URL") or DEFAULT_AGENT_URL
        token = os.environ.get(AGENT_TOKEN_ENV, "")
        success = agi_get_variable("SMS_REPORT_SUCCESS")
        if success in ("0", "1") and re.fullmatch(r"[0-9a-f]{32}", text):
            path = (
                f"/v1/sms/{text}/delivered" if success == "1"
                else f"/v1/sms/{text}/failed"
            )
            ok, detail = post_json(
                url, {"Authorization": f"Bearer {token}"}, path, {}
            )
            if ok:
                outcome = "delivered" if success == "1" else "failed"
                _log(f"Delivery report: sms {text} -> {outcome} (from {sender})")
                _respond(f"resolved={outcome}")
            else:
                _log(
                    f"ERROR: failed to record delivery report for "
                    f"sms {text}: {detail}"
                )
                _respond(f"error={detail}")
            return
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
