"""Userbot HTTP server — receives incoming events from Asterisk hooks.

On the Telegram node. The GSM node's Asterisk hooks (tg-sms-forward.sh)
POST to this server over Tailscale. Replaces the shell-command reverse path.
"""

from __future__ import annotations

import hmac
import os
import tempfile
import uuid
from datetime import datetime, timezone
from logging import getLogger
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request

from core.acl import ACLManager
from core.audit import AuditLogger
from core.contacts import ContactResolver
from core.errors import SMSErrorType
from core.events import EventType, SMSEvent
from core.logging_config import set_correlation

logger = getLogger("simbridge.userbot.http")


def create_http_server(
    secret: str,
    allowed_peers: list[str],
    acl: ACLManager,
    audit: AuditLogger,
    contacts: Optional[ContactResolver] = None,
    client=None,
    master_id: Optional[int] = None,
    metrics=None,
) -> FastAPI:
    """Create the HTTP server for receiving Asterisk events.

    *client* is the Telethon client (or a test double with an async
    ``send_message(entity, text)``) used to deliver events to Telegram
    users. When None, events are accepted and audited but not delivered
    (standalone/test mode).

    *master_id* (S06.2): the master user's Telegram ID — the recipient
    of alerts forwarded from the agent node. *metrics* (S06.2): the
    userbot-side MetricsCollector (incoming SMS, exported at /health).
    """

    app = FastAPI(title="SimBridge Userbot HTTP")
    app.state.expected_secret = secret
    app.state.allowed_peers = allowed_peers
    app.state.acl = acl
    app.state.audit = audit
    app.state.contacts = contacts
    app.state.client = client
    app.state.master_id = master_id
    app.state.metrics = metrics

    # S06.2: correlation IDs on every request (same contract as the
    # agent app) so the userbot's JSON log lines join the same trace as
    # the agent's audit records via x-correlation-id.
    @app.middleware("http")
    async def set_request_correlation(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or uuid.uuid4().hex
        set_correlation(cid)
        response = await call_next(request)
        response.headers["x-correlation-id"] = cid
        return response

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
            # No Telegram text on RING: the bridge's native
            # incoming-call banner on the user's own account already
            # signals the call, so the duplicate "Входящий звонок"
            # line was removed at user request (2026-08-21). The
            # event is still audited (kind=ring,
            # outcome=suppressed).
            formatted_text = None
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
        # recipient must not break the rest. A RING has nothing to
        # deliver (suppressed above) — the audience is still computed
        # and audited, but no message is sent.
        audience = sorted(acl.users_with_right("in_call" if is_ring else "in_sms"))
        delivered: list[int] = []
        failed: list[int] = []
        if formatted_text is None:
            logger.info(
                "RING from %s: text notification suppressed "
                "(native TG incoming-call banner already signals the call)",
                phone,
            )
        elif client is None:
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

        # S06.2: count real incoming SMS. "RING <number>" is a
        # call notification (counted as a call, not an SMS) — same
        # audience split as the delivery above.
        if metrics is not None and not is_ring:
            metrics.sms_incoming()

        if formatted_text is None:
            outcome = "suppressed"
        elif delivered and not failed:
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
        S03.3: Uses ContactResolver for caller display name; the uploaded
        audio is written to a temp file for the voice note and deleted
        after the send attempt — on BOTH the success and the failure
        path, so no recording lives on this node longer than the send.

        Delivery (S03.3): the in_call audience (a voicemail is a voice
        event, same audience as the ring notification). "normal" sends
        the label text plus the audio as a Telegram voice note;
        "early_hangup" and "recording_missing" are text-only.
        """
        received_secret = req.headers.get("x-simbridge-secret", "")
        if not hmac.compare_digest(received_secret, secret):
            raise HTTPException(status_code=401, detail="Invalid secret")

        # IP allowlist (same as /events/sms and /events/delivery)
        client_host = req.client.host if req.client else None
        if allowed_peers and client_host not in allowed_peers:
            raise HTTPException(status_code=403, detail="IP not allowed")

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

        # Deliver to the in_call audience. Per-user isolation: one
        # failing recipient must not break the rest (same pattern as
        # /events/sms).
        audience = sorted(acl.users_with_right("in_call"))
        delivered: list[int] = []
        failed: list[int] = []

        temp_path: Optional[str] = None
        try:
            # Voice note only for a real recording: "normal" + an
            # upload with content. early_hangup / recording_missing
            # are text-only (S03.1: sub-threshold audio is a greeting
            # fragment plus silence — no caller content).
            has_audio = False
            if vm_type == "normal" and client is not None and audio_file is not None:
                content = await audio_file.read()
                if content:
                    fd, temp_path = tempfile.mkstemp(
                        suffix=Path(audio_file.filename or "").suffix or ".opus",
                    )
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(content)
                    has_audio = True
                    logger.info(
                        "Voicemail audio from %s: %s (%d bytes)",
                        norm, audio_file.filename, len(content),
                    )
                else:
                    logger.warning("voicemail upload from %s is empty", norm)
            if client is None:
                logger.warning(
                    "voicemail from %s accepted but no Telethon client "
                    "is wired — not delivered", norm,
                )
            for uid in audience:
                try:
                    # Label text first, then the voice note: the text
                    # carries the caller number + resolved name and
                    # must land even if the media send fails.
                    await client.send_message(uid, vm_label)
                    if has_audio:
                        await client.send_file(uid, temp_path, voice_note=True)
                    delivered.append(uid)
                except Exception as e:
                    failed.append(uid)
                    logger.warning(
                        "failed to deliver voicemail to user %s: %s", uid, e,
                    )
        finally:
            # S03.3: the uploaded recording does not outlive the send
            # attempt — deleted on the success AND the failure path.
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        if delivered and not failed:
            outcome = "ok"
        elif delivered:
            outcome = "partial"
        elif audience:
            outcome = "failed"
        else:
            outcome = "no_audience"

        # S03.1: the three notification shapes are distinguishable in
        # the audit (early_hangup gets its own event type).
        event_type = (
            EventType.VOICEMAIL_EARLY_HANGUP
            if vm_type == "early_hangup"
            else EventType.VOICEMAIL_RECEIVED
        )
        audit.log(
            event_type,
            outcome=outcome,
            correlation_id=correlation_id,
            details={
                "from": norm,
                "name": name,
                "voicemail_type": vm_type,
                "duration": duration,
                "audience": audience,
                "delivered_to": delivered,
                "has_audio": has_audio,
            },
        )

        return {
            "ok": True,
            "voicemail_type": vm_type,
            "label": vm_label,
            "delivered_to": delivered,
        }

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

    @app.post("/events/call")
    async def handle_call_event(req: Request):
        """Outgoing-call outcome from the agent (S04.3).

        The agent reports the GSM leg's outcome for a Telegram-initiated
        call and the userbot sends a separate localized message to the
        user who placed it. The message goes ONLY to that user
        (record.telegram_user_id) — a call outcome is personal.

        Expected JSON body::

            {"to": "+7...", "telegram_user_id": 123,
             "status": "answered" | "no_answer" | "busy" | "failed",
             "call_id": "..."}
        """
        received_secret = req.headers.get("x-simbridge-secret", "")
        if not hmac.compare_digest(received_secret, secret):
            raise HTTPException(status_code=401, detail="Invalid secret")

        client_host = req.client.host if req.client else None
        if allowed_peers and client_host not in allowed_peers:
            raise HTTPException(status_code=403, detail="IP not allowed")

        body = await req.json()
        to = str(body.get("to", ""))
        uid = int(body.get("telegram_user_id", 0) or 0)
        status = str(body.get("status", ""))
        call_id = str(body.get("call_id", ""))

        # S04.3: separate localized messages per GSM outcome.
        messages = {
            "answered": f"Соединено с {to}",
            "no_answer": f"Нет ответа: {to}",
            "busy": f"Занято: {to}",
            "failed": f"Ошибка сети: {to}",
        }
        text = messages.get(status)
        if text is None:
            logger.warning(
                "call event with unknown status %r (call_id=%s)", status, call_id
            )
            audit.log(
                EventType.CALL_HANGUP,
                telegram_user_id=uid,
                outcome="unknown_status",
                correlation_id=call_id,
                details={"to": to, "status": status},
            )
            return {"ok": True, "notified": False}

        notified = False
        if uid == 0:
            # Caller unknown — audit only, no one to notify.
            logger.info("call %s for %s: no caller to notify", status, to)
        elif client is None:
            logger.warning(
                "call %s for user %s not notified — no client", status, uid
            )
        else:
            try:
                await client.send_message(uid, text)
                notified = True
            except Exception as e:
                logger.warning(
                    "failed to notify user %s of call %s: %s", uid, status, e
                )

        audit.log(
            EventType.CALL_HANGUP,
            telegram_user_id=uid,
            outcome=status,
            correlation_id=call_id,
            details={"to": to, "notified": notified},
        )
        return {"ok": True, "notified": notified}

    @app.post("/events/alert")
    async def handle_alert_event(req: Request):
        """Alert from the agent node, forwarded to the master user (S06.2).

        The agent has no Telegram client of its own, so its AlertManager
        sends over the tailnet to this endpoint; this node owns the
        Telegram session and does the actual send. The message goes
        ONLY to the master user (alerts are for the system owner, not a
        broadcast audience).

        Expected JSON body::

            {"message": "..."}
        """
        received_secret = req.headers.get("x-simbridge-secret", "")
        if not hmac.compare_digest(received_secret, secret):
            raise HTTPException(status_code=401, detail="Invalid secret")

        client_host = req.client.host if req.client else None
        if allowed_peers and client_host not in allowed_peers:
            raise HTTPException(status_code=403, detail="IP not allowed")

        body = await req.json()
        message = str(body.get("message", "")).strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is empty")

        sent = False
        if master_id is None or client is None:
            logger.warning(
                "alert received but no master user / Telegram client — not sent"
            )
        else:
            try:
                await client.send_message(master_id, message)
                sent = True
            except Exception as e:
                logger.warning(
                    "failed to send alert to master %s: %s", master_id, e
                )

        audit.log(
            EventType.ALERT_SENT,
            telegram_user_id=master_id,
            outcome="ok" if sent else "failed",
            details={"message_preview": message[:200], "sent": sent},
        )
        return {"ok": True, "sent": sent}

    @app.get("/health")
    async def health():
        """Liveness + Telegram session state + metrics (S06.2).

        Unauthenticated on purpose (as the previous stub was): the
        response is operational state, not secrets, and the server
        binds to the tailnet interface only. The agent's peer check
        (core/health.py ``check_peer_node``) reads ``telegram_connected``
        from this body — which is also why the response carries the
        session state the supervisor needs.
        """
        connected = None
        if client is not None:
            try:
                connected = bool(client.is_connected)
            except Exception:
                connected = False
        return {
            "status": "ok" if connected is not False else "degraded",
            "telegram_connected": connected,
            "metrics": metrics.get_all() if metrics is not None else None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

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
