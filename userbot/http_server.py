"""Userbot HTTP server — receives incoming events from Asterisk hooks.

On the Telegram node. The GSM node's Asterisk hooks (tg-sms-forward.sh)
POST to this server over Tailscale. Replaces the shell-command reverse path.
"""

from __future__ import annotations

import hmac
import os
from logging import getLogger
from typing import Optional

from fastapi import FastAPI, HTTPException, Request

from core.acl import ACLManager
from core.audit import AuditLogger
from core.contacts import ContactResolver
from core.errors import SMSErrorType
from core.events import EventType, SMSEvent

logger = getLogger("simbridge.userbot.http")


def create_http_server(
    secret: str,
    allowed_peers: list[str],
    acl: ACLManager,
    audit: AuditLogger,
    contacts: Optional[ContactResolver] = None,
    client=None,
) -> FastAPI:
    """Create the HTTP server for receiving Asterisk events.

    *client* is the Telethon client (or a test double with an async
    ``send_message(entity, text)``) used to deliver events to Telegram
    users. When None, events are accepted and audited but not delivered
    (standalone/test mode).
    """

    app = FastAPI(title="SimBridge Userbot HTTP")
    app.state.expected_secret = secret
    app.state.allowed_peers = allowed_peers
    app.state.acl = acl
    app.state.audit = audit
    app.state.contacts = contacts
    app.state.client = client

    @app.post("/events/sms")
    async def handle_sms_event(req: Request):
        """Receive incoming SMS event from Asterisk hook.

        Expected JSON body::

            {"phone_number": "+7...", "text": "...", "modem_id": "gsm"}
        """
        # Auth: check secret (timing-safe comparison)
        received_secret = req.headers.get("x-simbridge-secret", "")
        if not hmac.compare_digest(received_secret, secret):
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

        # D2: route by event kind. "RING <number>" is an incoming-call
        # notification (in_call audience) — production parity with the
        # old dialplan; everything else is a real SMS (in_sms audience).
        is_ring = sms_event.text == "RING" or sms_event.text.startswith("RING ")
        phone = sms_event.phone_number

        if is_ring:
            name = contacts.resolve(phone) if contacts else None
            formatted_text = (
                f"📞 Входящий звонок: {name} ({phone})"
                if name
                else f"📞 Входящий звонок: {phone}"
            )
        else:
            # S02.1: the sender number is ALWAYS part of the message
            # (legacy parity — "SMS +7...: text"); the contact name is
            # added when the resolver has one.
            name = contacts.resolve(phone) if contacts else None
            if name:
                formatted_text = f"SMS {phone} ({name}):\n{sms_event.text}"
            else:
                formatted_text = f"SMS {phone}:\n{sms_event.text}"

        logger.info(
            "Received %s from %s: %s... (len=%d)",
            "RING" if is_ring else "SMS",
            phone,
            sms_event.text[:30],
            len(sms_event.text),
        )

        # Deliver to the audience. Per-user isolation: one failing
        # recipient must not break the rest.
        audience = sorted(acl.users_with_right("in_call" if is_ring else "in_sms"))
        delivered: list[int] = []
        failed: list[int] = []
        if client is None:
            logger.warning(
                "SMS event accepted but no Telethon client is wired — not delivered"
            )
        else:
            for uid in audience:
                try:
                    await client.send_message(uid, formatted_text)
                    delivered.append(uid)
                except Exception as e:
                    failed.append(uid)
                    logger.warning("failed to deliver to user %s: %s", uid, e)

        if delivered and not failed:
            outcome = "ok"
        elif delivered:
            outcome = "partial"
        elif audience:
            outcome = "failed"
        else:
            outcome = "no_audience"

        audit.log(
            EventType.SMS_RECEIVED,
            outcome=outcome,
            correlation_id=sms_event.correlation_id,
            modem_id=sms_event.modem_id,
            details={
                "from": phone,
                "text_len": len(sms_event.text),
                "kind": "ring" if is_ring else "sms",
                "audience": audience,
                "delivered_to": delivered,
            },
        )

        return {
            "ok": True,
            "correlation_id": sms_event.correlation_id,
            "formatted_text": formatted_text,
            "delivered_to": delivered,
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
        if not hmac.compare_digest(received_secret, secret):
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

    @app.post("/events/delivery")
    async def handle_delivery_event(req: Request):
        """Delivery-state notification from the agent (D4).

        The agent resolves a carrier delivery report against its
        correlation store and POSTs the outcome here. The message goes
        ONLY to the user who sent the SMS (record.telegram_user_id) —
        a delivery status is personal, not a broadcast.
        """
        received_secret = req.headers.get("x-simbridge-secret", "")
        if not hmac.compare_digest(received_secret, secret):
            raise HTTPException(status_code=401, detail="Invalid secret")

        client_host = req.client.host if req.client else None
        if allowed_peers and client_host not in allowed_peers:
            raise HTTPException(status_code=403, detail="IP not allowed")

        body = await req.json()
        sms_id = str(body.get("sms_id", ""))
        phone = str(body.get("phone_number", ""))
        uid = int(body.get("telegram_user_id", 0) or 0)
        status = str(body.get("status", ""))
        error = body.get("error")

        if status == "delivered":
            text = f"Доставлено: {phone}"
        elif status == "failed":
            text = f"{SMSErrorType.DELIVERY_FAILED.value}: {phone}"
            if error:
                text += f"\n{error[:200]}"
        else:
            logger.warning(
                "delivery event with unknown status %r (sms_id=%s)", status, sms_id
            )
            audit.log(
                EventType.SMS_DELIVERY_REPORT,
                outcome="unknown_status",
                correlation_id=sms_id,
                details={"phone": phone, "status": status},
            )
            return {"ok": True, "notified": False}

        notified = False
        if uid == 0:
            # Sender unknown — audit only, no one to notify.
            logger.info(
                "delivery %s for %s: no sender to notify (sms_id=%s)",
                status, phone, sms_id,
            )
        elif client is None:
            logger.warning(
                "delivery %s for user %s not notified — no client", status, uid
            )
        else:
            try:
                await client.send_message(uid, text)
                notified = True
            except Exception as e:
                logger.warning(
                    "failed to notify user %s of delivery %s: %s", uid, status, e
                )

        audit.log(
            EventType.SMS_DELIVERY_REPORT,
            telegram_user_id=uid,
            outcome=status,
            correlation_id=sms_id,
            details={"phone": phone, "error": error, "notified": notified},
        )
        return {"ok": True, "notified": notified}

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
