#!/usr/bin/env python3
"""AGI script: notify simbridge-agent about incoming call events.

Called from extensions.conf (dialplan) via AGI() when an incoming call arrives
or when GSM dialing starts. Reads key-value pairs from stdin (AGI protocol),
calls the agent's call control API, and returns.

Usage in dialplan:
    AGI(notify-agent-agi.py,incoming,+7XXXXXXXXXX,channel-uniqueid)
    AGI(notify-agent-agi.py,gsm-dialing,+7XXXXXXXXXX,channel-uniqueid)

S04.3: Incoming call flow — the GSM channel is NOT answered until the
Telegram user accepts. This script registers the call with the agent API
so the userbot can start ringing Telegram.

Security: communicates with the agent over localhost/Tailscale using
the configured agent token. No user data is shell-interpolated.
"""

from __future__ import annotations

import os
import sys
import json

import requests


def read_agi_env() -> dict[str, str]:
    """Read AGI environment variables from stdin.

    Asterisk sends key-value pairs over stdin, terminated by a blank line.
    Returns a dict of the received variables.
    """
    env: dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\r\n")
        if not line:
            break  # blank line = end of headers
        if ": " in line:
            key, _, value = line.partition(": ")
            env[key.strip()] = value.strip()
    return env


def main() -> None:
    if len(sys.argv) < 3:
        print("Response: Follows\r\n")
        print("200 error=usage: notify-agent-agi.py <event> <phone> [channel_id]\r\n")
        print("\r\n")
        sys.exit(1)

    event = sys.argv[1]       # "incoming", "gsm-dialing", "gsm-busy", "gsm-no-answer"
    phone = sys.argv[2]
    channel_id = sys.argv[3] if len(sys.argv) > 3 else ""

    # Read AGI environment (channel info from Asterisk)
    agi_env = read_agi_env()

    # Config from environment (set by systemd / Asterisk)
    agent_url = os.environ.get("AGENT_URL", "http://127.0.0.1:8090")
    agent_token = os.environ.get("AGENT_TOKEN", "")

    # Contact name — resolve from agent (if available)
    contact_name = None

    headers = {"Content-Type": "application/json"}
    if agent_token:
        headers["Authorization"] = f"Bearer {agent_token}"

    try:
        if event == "incoming":
            # Register incoming call with agent API
            resp = requests.post(
                f"{agent_url}/v1/call/incoming",
                json={
                    "phone_number": phone,
                    "contact_name": contact_name,
                },
                headers=headers,
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                call_id = data.get("call_id", "")
                # Set channel variable so dialplan can reference the call
                print(f"SET VARIABLE CALL_ID {call_id}\r\n")
                print("\r\n")
            else:
                print(f"200 error=agent returned {resp.status_code}\r\n")
                print("\r\n")

        elif event == "gsm-dialing":
            # Notify agent that GSM dialing has started for an outgoing call
            # The call should already be registered via the agent API
            # (created by the userbot when the user requested the call)
            print(f"200 event={event} phone={phone}\r\n")
            print("\r\n")

        elif event in ("gsm-busy", "gsm-no-answer"):
            # GSM outcome notification — inform agent of dial result
            print(f"200 event={event} phone={phone}\r\n")
            print("\r\n")

        else:
            print(f"200 unknown event={event}\r\n")
            print("\r\n")

    except requests.RequestException as e:
        print(f"200 error={e}\r\n")
        print("\r\n")


if __name__ == "__main__":
    main()
