"""Userbot HTTP server — receives incoming events from Asterisk hooks.

On the Telegram node. The GSM node's Asterisk hooks (tg-sms-forward.sh)
POST to this server over Tailscale. Replaces the shell-command reverse path.
"""

from __future__ import annotations

import os
from logging import getLogger
from typing import Optional

from fastapi import FastAPI, HTTPException, Request

from core.acl import ACLManager
from core.audit import AuditLogger
from core.contacts import ContactResolver
from core.events import EventType, SMSEvent

logger = getLogger("simbridge.userbot.http")


def create_http_server(
    secret: str,
    allowed_peers: list[str],
    acl: ACLManager,
    audit: AuditLogger,
    contacts: Optional[ContactResolver] = None,
) -> FastAPI:
    """Create the HTTP server for receiving Asterisk events."""

    app = FastAPI(title="SimBridge Userbot HTTP")
    app.state.expected_secret = secret
    app.state.allowed_peers = allowed_peers
    app.state.acl = acl
    app.state.audit = audit
    app.state.contacts = contacts

    @app.post("/events/sms")
    async def handle_sms_event(req: Request):
        """Receive incoming SMS event from Asterisk hook.

        Expected JSON body::

            {"phone_number": "+7...", "text": "...", "modem_id": "gsm"}
        """
        # Auth: check secret header
        received_secret = req.headers.get("x-simbridge-secret", "")
        if received_secret != secret:
            raise HTTPException(status_code=401, detail="Invalid secret")

        # IP allowlist
        client_host = req.client.host if req.client else None
        if allowed_peers and client_host not in allowed_peers:
            raise HTTPException(status_code=403, detail="IP not allowed")

        body = await req.json()
        sms_event = SMSEvent(
            phone_number=body["phone_number"],
            text=body["text"],
            modem_id=body.get("modem_id", "gsm"),
        )

        # S02.1: Format with contact name if available
        formatted_text = sms_event.text
        if contacts:
            from core.phone import normalize_e164
            name = contacts.resolve(sms_event.phone_number)
            if name:
                formatted_text = f"SMS {sms_event.phone_number} ({name}):\n{sms_event.text}"
            else:
                formatted_text = f"SMS {sms_event.phone_number}:\n{sms_event.text}"

        # Forward to Telegram users who have in_sms right
        # (This would call the Telethon client — wired via app state)
        logger.info(
            "Received SMS from %s: %s... (len=%d)",
            sms_event.phone_number,
            sms_event.text[:30],
            len(sms_event.text),
        )

        # Audit
        audit.log(
            EventType.SMS_SUBMITTED,
            outcome="ok",
            correlation_id=sms_event.correlation_id,
            modem_id=sms_event.modem_id,
            details={"from": sms_event.phone_number, "text_len": len(sms_event.text)},
        )

        return {
            "ok": True,
            "correlation_id": sms_event.correlation_id,
            "formatted_text": formatted_text,
        }

    @app.post("/events/voicemail")
    async def handle_voicemail_event(req: Request):
        """Receive voicemail notification from Asterisk hook."""
        received_secret = req.headers.get("x-simbridge-secret", "")
        if received_secret != secret:
            raise HTTPException(status_code=401, detail="Invalid secret")

        body = await req.json()
        logger.info("Received voicemail from %s", body.get("phone_number"))
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


def main() -> None:
    """Run the HTTP server standalone (for testing)."""
    import uvicorn

    cfg_path = os.environ.get("SIMBRIDGE_CONFIG", "/etc/simbridge/simbridge.yaml")
    from core.config import load_config

    cfg = load_config(cfg_path)
    secret = os.environ[cfg["userbot_http.secret_env"]]

    acl = ACLManager(cfg["telegram.acl_file"])
    audit = AuditLogger(cfg["paths.audit_log"])

    app = create_http_server(
        secret=secret,
        allowed_peers=cfg.get("userbot_http.allowed_peers", []),
        acl=acl,
        audit=audit,
    )

    listen_addr = cfg["userbot_http.listen"]
    uvicorn.run(app, host=listen_addr.split(":")[0], port=int(listen_addr.split(":")[1]))
