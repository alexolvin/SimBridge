"""Bridge control client (S04.3) — the voice bridge's HTTP control API.

The userbot and the bridge run side by side on the Telegram node, so
the control API is loopback-only (127.0.0.1:<voice.bridge_control_port>)
and is never exposed on the Tailscale interface. Loopback binding is an
architectural invariant (the bridge holds the Telegram session), not a
configurable; the port is configurable.

Contract required of the bridge (blitss/sip-tg-bridge is a POC — its
control API is adapted/forked to match this; see docs/voice-bridge.md):

    POST /call    {"user_id": int, "target": "<E.164>",
                   "gsm_host": "<SIP host of the GSM node>",
                   "gsm_port": 5060}
        → 2xx: the bridge starts a Telegram call to *user_id*; when the
          user accepts, the bridge INVITEs sip:<target>@<gsm_host>:<gsm_port>
          (From-user = the bridge endpoint, Request-URI user = target),
          which lands in Asterisk context <bridge_endpoint> and dials
          the Dongle.
        → non-2xx / unreachable: no Telegram call was started.
    POST /cancel  {"user_id": int}
        → 2xx: the in-progress Telegram ring/call for *user_id* is
          cancelled.

``gsm_host`` is derived from ``agent.listen`` (the userbot reaches the
GSM node's agent there) — no separate config key (Rule 1).

Auth: Bearer token from the environment variable named by
``userbot_http.secret_env`` (SIMBRIDGE_HTTP_SECRET) — the same secret
domain as the agent → userbot event channel.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

# Asterisk PJSIP standard SIP port — architectural invariant, not config.
ASTERISK_SIP_PORT = 5060


class BridgeControl:
    """Loopback HTTP client for the voice bridge control API."""

    def __init__(self, cfg: dict):
        port = int(cfg.get("voice.bridge_control_port", 5063))
        self._base_url = f"http://127.0.0.1:{port}"
        secret_env = cfg.get("userbot_http.secret_env", "SIMBRIDGE_HTTP_SECRET")
        self._token = os.environ.get(secret_env, "")
        # GSM node SIP host: the userbot's agent URL is the GSM node.
        self._gsm_host = cfg.get("agent.listen", "127.0.0.1:8090").split(":")[0]

    def _headers(self) -> dict:
        return (
            {"Authorization": f"Bearer {self._token}"}
            if self._token
            else {}
        )

    async def start_call(self, user_id: int, target: str) -> bool:
        """Start a Telegram call to *user_id* targeting *target*.

        Returns True only on a 2xx — otherwise no Telegram call is
        being ringed and the caller must close the agent-side call.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.post(
                    f"{self._base_url}/call",
                    json={
                        "user_id": int(user_id),
                        "target": target,
                        "gsm_host": self._gsm_host,
                        "gsm_port": ASTERISK_SIP_PORT,
                    },
                    headers=self._headers(),
                )
                return 200 <= resp.status_code < 300
        except httpx.HTTPError:
            return False

    async def cancel_call(self, user_id: Optional[int] = None) -> bool:
        """Cancel the in-progress Telegram ring/call for *user_id*
        (or the single active one when *user_id* is None)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.post(
                    f"{self._base_url}/cancel",
                    json={"user_id": int(user_id)} if user_id else {},
                    headers=self._headers(),
                )
                return 200 <= resp.status_code < 300
        except httpx.HTTPError:
            return False
