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
        """Receive voicemail from Asterisk hook (tg-voice-forward.sh).

        Expects multipart form-data:
        - file: normalized audio (opus/ogg)
        - phone_number: caller E.164 number
        - voicemail_type: "normal" | "early_hangup" | "recording_missing"
        - correlation_id: Asterisk UNIQUEID
        - duration: recording duration in seconds

        S03.1: Distinguishes early hangup from normal voicemail.
        S03.3: Uses ContactResolver for caller display name.
        """
        received_secret = req.headers.get("x-simbridge-secret", "")
        if received_secret != secret:
            raise HTTPException(status_code=401, detail="Invalid secret")

        # Parse multipart form-data
        form = await req.form()
        audio_file = form.get("file")
        phone_number = form.get("phone_number", "unknown")
        vm_type = form.get("voicemail_type", "normal")
        correlation_id = form.get("correlation_id", "")
        duration = form.get("duration", "0")

        # S03.1: Resolve contact name (S02 feature)
        from core.phone import normalize_e164
        norm = normalize_e164(phone_number) or phone_number
        name = None
        if contacts:
            name = contacts.resolve(norm)

        # Build Telegram notification text
        if vm_type == "early_hangup":
            vm_label = "📞 Звонок (брёл)"  # called, hung up during greeting
            if name:
                vm_label = f"📞 Звонок — {name} ({norm})"
            else:
                vm_label = f"📞 Звонок — {norm}"
        elif vm_type == "recording_missing":
            vm_label = f"⚠️ Нет записи — {norm}"
            if name:
                vm_label = f"⚠️ Нет записи — {name} ({norm})"
        else:  # normal
            vm_label = f"🎙 Голосовое — {norm}"
            if name:
                vm_label = f"🎙 Голосовое — {name} ({norm})"

        logger.info(
            "Received voicemail from %s type=%s duration=%ss correlation=%s",
            norm, vm_type, duration, correlation_id,
        )

        # Audit log
        event_type = EventType.VOICEMAIL_EARLY_HANGUP if vm_type == "early_hangup" else EventType.VOICEMAIL_RECEIVED
        audit.log(
            event_type,
            outcome="ok",
            correlation_id=correlation_id,
            details={
                "from": norm,
                "name": name,
                "voicemail_type": vm_type,
                "duration": duration,
            },
        )

        # TODO: Send to Telegram via Telethon client (wired via app state)
        # The audio file is available as audio_file (UploadFile)
        # Notification text: vm_label
        if audio_file:
            logger.info("Voicemail audio received: %s (%d bytes)", audio_file.filename, len(audio_file.file.read(0) if hasattr(audio_file.file, 'read') else 0))

        return {"ok": True, "voicemail_type": vm_type, "label": vm_label}

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
