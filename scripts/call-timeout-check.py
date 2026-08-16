#!/usr/bin/env python3
"""Call timeout driver (S04.3).

Triggered by the simbridge-timeouts systemd timer (~5 s period).
POSTs /v1/call/check-timeouts to the local agent, which reaps calls
past their windows:

  - RINGING / TELEGRAM_RINGING past ring_wait_seconds -> voicemail;
  - MODEM_RESERVED / TELEGRAM_CALLING past the outbound answer
    timeout -> TELEGRAM_TIMEOUT + user notified;
  - BRIDGED past max_call_seconds -> AMI hangup of the active legs.

The outgoing Telegram ring is out-of-band (no dialplan Dial enforces
it), so this poller is the ONLY thing that can expire it — hence the
short period. The endpoint is idempotent: with no overdue call it is
a no-op, so the steady-state cost is one small HTTP POST per tick.

Runs under the agent venv (httpx + PyYAML for config). The agent
token comes from the process environment (the unit's
EnvironmentFile=/etc/simbridge/env) — never from the YAML.

A transient agent outage is a warning, not a failure: the unit stays
green and the next tick retries (a failing oneshot every 5 s would
flood the journal during a normal agent restart). A config error
(missing token env, unreadable config) exits 1 — that is a real
misconfiguration and should be visible.

Usage:
    /opt/simbridge-venv/bin/python /opt/simbridge/scripts/call-timeout-check.py
"""

from __future__ import annotations

import os
import sys

# Python puts the SCRIPT's directory on sys.path, not the cwd — add
# the app root explicitly so `core` resolves however the script is
# invoked (the systemd unit, a manual run, a test). Same bootstrap
# pattern as sweep-recordings.py (Rule 1: one pattern).
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

import httpx

from core.config import load_config


def main() -> None:
    try:
        cfg = load_config(os.environ.get("SIMBRIDGE_CONFIG"))
        token = os.environ[cfg["agent.token_env"]]
    except Exception as e:
        print(f"call-timeout-check: config error: {e}", file=sys.stderr)
        sys.exit(1)

    url = f"http://{cfg['agent.listen']}/v1/call/check-timeouts"
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        r.raise_for_status()
    except (httpx.HTTPError, OSError) as e:
        # Agent briefly down — warn, keep the unit green, retry next tick.
        print(f"call-timeout-check: agent unreachable: {e}", file=sys.stderr)
        return

    data = r.json()
    if data.get("timed_out"):
        actions = [a.get("action") for a in data.get("actions", [])]
        print(f"call-timeout-check: reaped {data['timed_out']} "
              f"call(s): {actions}")


if __name__ == "__main__":
    main()
