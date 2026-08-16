"""Agent API routes — SMS, modem, health, blacklist endpoints.

Integrates contacts, blacklist, sms_correlation, and error surfaces (Stage 02).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from logging import getLogger
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field

from core.audit import AuditLogger
from core.events import EventType
from core.ratelimit import RateLimiter
from core.blacklist import BlacklistManager
from core.sms_correlation import SMSCorrelationStore
from core.errors import SMSErrorType, asterisk_sms_error_to_type
from core.acl import ACLManager
from agent.ami_client import AMIClient, AMISendError
from agent.deps import (
    require_auth,
    get_ami,
    get_cfg,
    get_audit,
    get_sms_limiter,
    get_blacklist,
    get_sms_store,
    get_call_registry,
    get_acl,
    get_call_limiter,
    get_metrics,
    get_health_checker,
)
from core.call_control import CallRegistry, CallState, InvalidTransition, ModemBusyError

router = APIRouter()
# Apply auth + IP allowlist + replay protection to all /v1 routes
router.dependencies.append(Depends(require_auth))

logger = getLogger("simbridge.agent")


# ---------------------------------------------------------------------------
# Request/response schemas
# ---------------------------------------------------------------------------

class SMSRequest(BaseModel):
    to: str = Field(..., description="Destination phone number (E.164)")
    text: str = Field(..., min_length=1, max_length=160, description="SMS body")
    correlation_id: Optional[str] = Field(None, description="Tracing ID")
    telegram_user_id: Optional[int] = Field(None, description="Sender for ACL")
    telegram_message_id: Optional[int] = Field(None, description="Telegram message for correlation")


class SMSResponse(BaseModel):
    ok: bool
    correlation_id: str
    sms_id: str
    message: str


class ModemInfo(BaseModel):
    device: str
    registered: bool
    signal_percent: Optional[int] = None
    operator: Optional[str] = None
    imei_suffix: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    asterisk_reachable: bool
    dongle_registered: Optional[bool] = None
    timestamp: str
    components: Optional[dict] = None
    metrics: Optional[dict] = None


class BlockRequest(BaseModel):
    number: str = Field(..., description="Phone number to block/unblock (E.164)")


class BlockResponse(BaseModel):
    ok: bool
    action: str  # "blocked" or "unblocked"
    number: str


# ---------------------------------------------------------------------------
# Call control schemas (S04.2)
# ---------------------------------------------------------------------------

class CallRequest(BaseModel):
    phone_number: str = Field(..., description="Phone number (E.164)")
    contact_name: Optional[str] = Field(None, description="Display name")
    gsm_channel_id: Optional[str] = Field(
        None, description="Asterisk channel name of the GSM leg (from AGI env)"
    )


class OutgoingCallRequest(BaseModel):
    phone_number: str = Field(..., description="Destination (E.164)")
    telegram_user_id: Optional[int] = Field(None, description="Sender for ACL")


class CallCompleteRequest(BaseModel):
    status: str = Field(
        ...,
        description=(
            "Final leg outcome: answered | no_answer | busy | cancelled | "
            "ended | failed (mapped from DIALSTATUS by the AGI)"
        ),
    )
    dialstatus: Optional[str] = Field(
        None, description="Raw DIALSTATUS from the dialplan (diagnostics)"
    )


class OutgoingAcceptedRequest(BaseModel):
    bridge_channel_id: str = Field(
        "", description="Asterisk channel name of the bridge INVITE leg"
    )


class CallResponse(BaseModel):
    call_id: str
    state: str
    caller_number: str
    callee_number: str
    direction: str


class CallStateResponse(BaseModel):
    call_id: str
    state: str
    direction: str
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/sms", response_model=SMSResponse)
async def send_sms(
    req: SMSRequest,
    request: Request,
    ami: AMIClient = Depends(get_ami),
    audit: AuditLogger = Depends(get_audit),
    limiter: RateLimiter = Depends(get_sms_limiter),
    blacklist: BlacklistManager = Depends(get_blacklist),
    sms_store: SMSCorrelationStore = Depends(get_sms_store),
    metrics_collector: "MetricsCollector" = Depends(get_metrics),
):
    """Send outgoing SMS via Asterisk + chan_dongle.

    **Security:** SMS text is passed as a parameter to the AMI client,
    never interpolated into a shell command (Rule 1).

    **Auth:** Requires valid bearer token + allowed peer IP (checked by
    router-level Depends(require_auth)).

    **S02:** Checks blacklist, creates correlation record, returns sms_id.
    """
    correlation_id = req.correlation_id or uuid.uuid4().hex
    cfg = get_cfg(request)
    dongle = cfg.get("asterisk.dongle", "gsm")

    # S02.2: Check if destination is blacklisted
    if blacklist.contains(req.to):
        audit.log(
            EventType.SMS_SEND_REQUESTED,
            telegram_user_id=req.telegram_user_id,
            outcome="blacklisted",
            correlation_id=correlation_id,
            details={"to": req.to},
        )
        raise HTTPException(
            status_code=403,
            detail=SMSErrorType.BLACKLISTED.message,
        )

    # Rate limit check (keyed by telegram_user_id or destination)
    limiter_key = f"sms:{req.telegram_user_id or req.to}"
    if not limiter.allow(limiter_key):
        limit_val = cfg.get("limits.sms_per_hour", 30)
        audit.log(
            EventType.SMS_SEND_REQUESTED,
            telegram_user_id=req.telegram_user_id,
            outcome="rate_limited",
            correlation_id=correlation_id,
            details={"to": req.to, "limit": limit_val},
        )
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit_val} SMS per hour",
        )

    # S02.3: Create correlation record
    record = sms_store.create(
        telegram_user_id=req.telegram_user_id or 0,
        phone_number=req.to,
        text=req.text,
        telegram_message_id=req.telegram_message_id,
        modem_id=dongle,
    )

    # Audit: request received
    audit.log(
        EventType.SMS_SEND_REQUESTED,
        telegram_user_id=req.telegram_user_id,
        outcome="submitted",
        correlation_id=correlation_id,
        details={"to": req.to, "sms_id": record.sms_id},
    )

    try:
        await ami.send_sms(req.to, req.text)
        sms_store.mark_submitted(record.sms_id)
    except AMISendError as e:
        # Asterisk explicitly refused the send (not registered, SIM
        # error, ...) — map to the categorized user-facing message.
        sms_store.mark_failed(
            record.sms_id,
            error=str(e),
            submit_failed=True,
        )
        metrics_collector.sms_failed()
        raise HTTPException(
            status_code=502,
            detail=asterisk_sms_error_to_type(str(e)).message,
        )
    except ConnectionError:
        sms_store.mark_failed(
            record.sms_id,
            error=SMSErrorType.MODEM_UNAVAILABLE.message,
            submit_failed=True,
        )
        metrics_collector.sms_failed()
        raise HTTPException(status_code=503, detail=SMSErrorType.MODEM_UNAVAILABLE.message)
    except Exception as e:
        err_msg = str(e)
        sms_store.mark_failed(
            record.sms_id,
            error=err_msg,
            submit_failed=True,
        )
        metrics_collector.sms_failed()
        raise HTTPException(status_code=500, detail=err_msg)

    # S06.2: "sent" means submitted to the modem; delivery is counted
    # when the report arrives (see /sms/report).
    metrics_collector.sms_sent()

    # Audit: submitted to modem
    audit.log(
        EventType.SMS_SUBMITTED,
        telegram_user_id=req.telegram_user_id,
        outcome="ok",
        correlation_id=correlation_id,
        modem_id=dongle,
        details={"to": req.to, "sms_id": record.sms_id},
    )

    return SMSResponse(
        ok=True,
        correlation_id=correlation_id,
        sms_id=record.sms_id,
        message=f"SMS submitted to {req.to}",
    )


@router.post("/blacklist", response_model=BlockResponse)
async def block_number(
    req: BlockRequest,
    request: Request,
    blacklist: BlacklistManager = Depends(get_blacklist),
    audit: AuditLogger = Depends(get_audit),
):
    """Add a number to the blacklist (BLOCK command).

    Atomic write — the file is never left in a partial state.
    """
    added = blacklist.block(req.number)
    audit.log(
        EventType.BLACKLIST_CHANGED,
        outcome="blocked",
        details={"number": req.number, "added": added},
    )
    return BlockResponse(
        ok=True,
        action="blocked",
        number=req.number,
    )


@router.post("/unblock", response_model=BlockResponse)
async def unblock_number(
    req: BlockRequest,
    request: Request,
    blacklist: BlacklistManager = Depends(get_blacklist),
    audit: AuditLogger = Depends(get_audit),
):
    """Remove a number from the blacklist (UNBLOCK command)."""
    removed = blacklist.unblock(req.number)
    audit.log(
        EventType.BLACKLIST_CHANGED,
        outcome="unblocked",
        details={"number": req.number, "removed": removed},
    )
    return BlockResponse(
        ok=True,
        action="unblocked",
        number=req.number,
    )


@router.post("/sms/{sms_id}/delivered")
async def report_sms_delivered(
    sms_id: str,
    sms_store: SMSCorrelationStore = Depends(get_sms_store),
):
    """Report that an SMS was delivered (from Asterisk delivery report).

    S02.3: Delivery matched by sms_id, not by text search.
    """
    found = sms_store.mark_delivered(sms_id)
    if not found:
        raise HTTPException(status_code=404, detail="SMS record not found")
    return {"ok": True, "sms_id": sms_id}


@router.post("/sms/{sms_id}/failed")
async def report_sms_failed(
    sms_id: str,
    sms_store: SMSCorrelationStore = Depends(get_sms_store),
):
    """Report that an SMS delivery failed (from Asterisk delivery report)."""
    found = sms_store.mark_failed(sms_id, error="delivery_failed")
    if not found:
        raise HTTPException(status_code=404, detail="SMS record not found")
    return {"ok": True, "sms_id": sms_id}


class SMSReportRequest(BaseModel):
    phone_number: str = Field("", description="Originator of the report SMS (carrier short code)")
    text: str = Field(..., min_length=1, description="Raw carrier delivery report text")
    modem_id: str = Field("gsm", description="Dongle that received the report")


@router.post("/sms/report")
async def sms_delivery_report(
    req: SMSReportRequest,
    request: Request,
    sms_store: SMSCorrelationStore = Depends(get_sms_store),
    audit: AuditLogger = Depends(get_audit),
    metrics_collector: "MetricsCollector" = Depends(get_metrics),
):
    """Carrier delivery report (DongleSendSMS Report=yes).

    The AGI hook (tg-sms-agi.py, report mode) forwards the raw report
    text here with the bearer token. The record is matched by
    phone-number hint within the report, else the newest pending record
    on the same dongle; resolved as delivered/failed by keywords, then
    announced to the userbot (best effort).

    **S02.3:** Reports are correlated by content + dongle, because
    chan_dongle delivery reports carry no reference to the original SMS.
    """
    cfg = get_cfg(request)
    record = sms_store.match_report(req.modem_id, req.text)
    if record is None:
        audit.log(
            EventType.SMS_DELIVERY_REPORT,
            outcome="no_match",
            details={
                "modem_id": req.modem_id,
                "from": req.phone_number,
                "text_preview": req.text[:120],
            },
        )
        return {"ok": True, "matched": False}

    lowered = req.text.lower()
    failed_markers = (
        "not delivered", "не доставлен", "expired", "timeout", "failed",
    )
    if any(m in lowered for m in failed_markers):
        sms_store.mark_failed(record.sms_id, error=req.text[:200])
        status = "failed"
        metrics_collector.sms_failed()
    else:
        sms_store.mark_delivered(record.sms_id)
        status = "delivered"
        metrics_collector.sms_delivered()

    audit.log(
        EventType.SMS_DELIVERY_REPORT,
        telegram_user_id=record.telegram_user_id,
        outcome=status,
        details={
            "sms_id": record.sms_id,
            "phone": record.phone_number,
            "from": req.phone_number,
        },
    )

    await _notify_userbot_delivery(
        cfg,
        sms_id=record.sms_id,
        phone_number=record.phone_number,
        telegram_user_id=record.telegram_user_id,
        status=status,
        error=req.text[:200] if status == "failed" else None,
    )

    return {"ok": True, "matched": True, "sms_id": record.sms_id, "status": status}


async def _notify_userbot_delivery(
    cfg,
    *,
    sms_id: str,
    phone_number: str,
    telegram_user_id: int,
    status: str,
    error: Optional[str],
) -> None:
    """Announce a resolved delivery state to the userbot (best effort).

    The userbot notifies the original sender. Any failure is logged and
    swallowed — a delivery report must never fail because the
    notification did.
    """
    import httpx

    try:
        url = cfg["agent.userbot_url"].rstrip("/") + "/events/delivery"
        secret = os.environ.get(
            cfg.get("userbot_http.secret_env", "SIMBRIDGE_HTTP_SECRET"), ""
        )
    except KeyError:
        logger.warning("delivery notification skipped: config incomplete")
        return

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                url,
                json={
                    "sms_id": sms_id,
                    "phone_number": phone_number,
                    "telegram_user_id": telegram_user_id,
                    "status": status,
                    "error": error,
                },
                headers={"X-SimBridge-Secret": secret},
            )
        if resp.status_code >= 400:
            logger.warning(
                "userbot delivery notification rejected: %s %s",
                resp.status_code,
                resp.text[:120],
            )
    except Exception as e:
        logger.warning("userbot delivery notification failed: %s", e)


async def _notify_userbot_call(cfg, *, call, status: str) -> None:
    """Announce an outgoing call outcome to the userbot (best effort).

    The userbot sends a separate localized message to the calling user
    (S04.3: answered / no_answer / busy / failed). Any failure is logged
    and swallowed — a call outcome must never fail because the
    notification did.
    """
    import httpx

    try:
        url = cfg["agent.userbot_url"].rstrip("/") + "/events/call"
        secret = os.environ.get(
            cfg.get("userbot_http.secret_env", "SIMBRIDGE_HTTP_SECRET"), ""
        )
    except KeyError:
        logger.warning("call notification skipped: config incomplete")
        return

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                url,
                json={
                    "to": call.callee_number,
                    "telegram_user_id": call.telegram_user_id,
                    "status": status,
                    "call_id": call.call_id,
                },
                headers={"X-SimBridge-Secret": secret},
            )
        if resp.status_code >= 400:
            logger.warning(
                "userbot call notification rejected: %s %s",
                resp.status_code,
                resp.text[:120],
            )
    except Exception as e:
        logger.warning("userbot call notification failed: %s", e)


@router.get("/modems", response_model=list[ModemInfo])
async def get_modems(
    ami: AMIClient = Depends(get_ami),
):
    """Query modem registration and signal state."""
    try:
        status = await ami.get_modem_status()
        return [ModemInfo(
            device=status.get("device") or "unknown",
            registered=status.get("registered", False),
            signal_percent=status.get("signal_percent"),
            operator=status.get("operator"),
            imei_suffix=status.get("imei_suffix"),
        )]
    except ConnectionError:
        raise HTTPException(status_code=503, detail="Asterisk AMI unreachable")


# ---------------------------------------------------------------------------
# Call control endpoints (S04.2)
# ---------------------------------------------------------------------------

@router.post("/call/incoming", response_model=CallResponse)
async def call_incoming(
    req: CallRequest,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    audit: AuditLogger = Depends(get_audit),
):
    """Register an incoming GSM → Telegram call.

    S04.3: The GSM channel is NOT answered — the caller hears real ringback
    while we ring Telegram. The user must accept before we answer the GSM leg.

    Stage 04: the dialplan rings the bridge on the very next line, so the
    TELEGRAM_RINGING transition happens here (one logical step — there is
    no decision between the two and no separate telegram-ring event).
    """
    call = registry.create_incoming(
        caller_number=req.phone_number,
        caller_name=req.contact_name,
        # S05.1: provenance — the node's configured dongle, not a default.
        modem_id=get_cfg(request).get("asterisk.dongle", "gsm"),
        gsm_channel_id=req.gsm_channel_id,
    )
    registry.start_telegram_ring(call.call_id)
    audit.log(
        EventType.CALL_INCOMING,
        outcome="ok",
        correlation_id=call.call_id,
        details={"from": req.phone_number, "gsm_channel": req.gsm_channel_id},
    )
    return CallResponse(
        call_id=call.call_id,
        state=call.state.value,
        caller_number=call.caller_number,
        callee_number=call.callee_number,
        direction=call.direction,
    )


@router.post("/call/outgoing", response_model=CallResponse)
async def call_outgoing(
    req: OutgoingCallRequest,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    blacklist: BlacklistManager = Depends(get_blacklist),
    acl: ACLManager = Depends(get_acl),
    audit: AuditLogger = Depends(get_audit),
    call_limiter: RateLimiter = Depends(get_call_limiter),
):
    """Register an outgoing Telegram → GSM call.

    S04.3: ACL is checked BEFORE any call session is created.
    Never call the user first and authorize afterwards.

    S06.1: Rate-limited per user via limits.calls_per_minute.
    """
    # S06.1: Rate limit calls per user
    limiter_key = f"call:{req.telegram_user_id or 0}"
    if not call_limiter.allow(limiter_key):
        cfg = get_cfg(request)
        limit_val = cfg.get("limits.calls_per_minute", 3)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {limit_val} calls per minute",
        )

    # ACL check — before any call session (GPT §26)
    if req.telegram_user_id and not acl.check(req.telegram_user_id, "out_call"):
        audit.log(
            EventType.CALL_ACL_CHECK,
            telegram_user_id=req.telegram_user_id,
            outcome="denied",
            details={"to": req.phone_number},
        )
        raise HTTPException(
            status_code=403,
            detail="Not authorized to place outgoing calls",
        )
    audit.log(
        EventType.CALL_ACL_CHECK,
        telegram_user_id=req.telegram_user_id,
        outcome="allowed",
        details={"to": req.phone_number},
    )

    if blacklist.contains(req.phone_number):
        raise HTTPException(
            status_code=403,
            detail=SMSErrorType.BLACKLISTED.message,
        )
    try:
        call = registry.create_outgoing(
            callee_number=req.phone_number,
            caller_number=f"user:{req.telegram_user_id or 0}",
            telegram_user_id=req.telegram_user_id,
        )
    except ModemBusyError as exc:
        # TS05-4: all-busy/offline must be a clear message, not a hang.
        detail = (
            "Modem offline — check the device"
            if exc.reason == "offline"
            else "Modem busy — another call in progress"
        )
        raise HTTPException(status_code=503, detail=detail)
    # S04.3: the userbot starts the Telegram ring via the bridge control
    # API immediately after this response. Mark the ring as started now —
    # the Telegram ring is out-of-band (no dialplan Dial enforces it), so
    # /call/check-timeouts is the only thing that can expire it.
    registry.start_telegram_calling(call.call_id)
    audit.log(
        EventType.CALL_OUTGOING,
        telegram_user_id=req.telegram_user_id,
        outcome="ok",
        correlation_id=call.call_id,
        details={"to": req.phone_number},
    )
    return CallResponse(
        call_id=call.call_id,
        state=call.state.value,
        caller_number=call.caller_number,
        callee_number=call.callee_number,
        direction=call.direction,
    )


@router.post("/call/outgoing/accepted", response_model=CallStateResponse)
async def call_outgoing_accepted(
    req: OutgoingAcceptedRequest,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    audit: AuditLogger = Depends(get_audit),
):
    """The Telegram user accepted an outgoing call; the bridge INVITEd us.

    S04.3: Outgoing. The bridge (UAC side) delivers the accept as a SIP
    INVITE with the target number as the Request-URI user; the [tg-bridge]
    dialplan calls this before dialing the Dongle. A 404 (call already
    expired by the TG ring timeout) leaves the AGI's CALL_ID unset, which
    gates the GSM dial (nocal) — no stray call to the target.
    """
    with registry._lock:
        pending = [
            c for c in registry._calls.values()
            if c.direction == "outgoing"
            and c.state in (CallState.MODEM_RESERVED, CallState.TELEGRAM_CALLING)
        ]
    if not pending:
        raise HTTPException(status_code=404, detail="No pending outgoing call")
    if len(pending) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"Ambiguous: {len(pending)} pending outgoing calls",
        )
    call = pending[0]
    if call.state == CallState.MODEM_RESERVED:
        registry.start_telegram_calling(call.call_id)
    if req.bridge_channel_id:
        registry.set_bridge_leg(call.call_id, req.bridge_channel_id)
    if not registry.user_accepted(call.call_id):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot accept — call is in {call.state.value} state",
        )
    if not registry.dial_gsm(call.call_id):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot dial GSM — call is in {call.state.value} state",
        )
    audit.log(
        EventType.CALL_ACCEPTED,
        telegram_user_id=call.telegram_user_id,
        correlation_id=call.call_id,
        outcome="ok",
        details={
            "to": call.callee_number,
            "direction": "outgoing",
            "bridge_channel": req.bridge_channel_id,
        },
    )
    return CallStateResponse(
        call_id=call.call_id,
        state=call.state.value,
        direction=call.direction,
    )


@router.post("/call/{call_id}/complete")
async def call_complete(
    call_id: str,
    req: CallCompleteRequest,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    ami: AMIClient = Depends(get_ami),
    audit: AuditLogger = Depends(get_audit),
):
    """Report the final outcome of a call leg from the dialplan (S04.3).

    Dial() blocks for the whole call and returns a final DIALSTATUS, so
    the dialplan reports outcomes in one or two events (s-exten with the
    mapped status, h-exten with explicit ENDED). The second POST hits a
    terminal state and gets 404 — the AGI treats that as a no-op.
    """
    call = registry.get(call_id)
    if not call or call.is_terminal:
        raise HTTPException(
            status_code=404, detail="Call not found or already closed"
        )

    cfg = get_cfg(request)

    # --- BRIDGED (either direction): the call is ending ---
    if call.state == CallState.BRIDGED:
        if req.status == "answered":
            raise HTTPException(status_code=409, detail="Call already bridged")
        # ended / cancelled / failed: terminate any surviving leg
        # (no-op if the dialplan's hanguptree already did it) and close.
        for channel_id in call.get_active_channel_ids():
            try:
                await ami.hangup_channel(channel_id, reason="BYE")
            except Exception as e:
                audit.log(
                    EventType.CALL_HANGUP,
                    correlation_id=call_id,
                    outcome="partial_hangup",
                    details={"error": str(e), "channel": channel_id},
                )
        registry.hangup(
            call_id,
            reason="completed" if req.status == "ended" else req.status,
        )
        audit.log(
            EventType.CALL_HANGUP,
            telegram_user_id=call.telegram_user_id,
            correlation_id=call_id,
            outcome=req.status,
            details={
                "direction": call.direction,
                "dialstatus": req.dialstatus,
            },
        )
        registry.cleanup(call_id)
        return {"ok": True, "call_id": call_id, "state": "cleanup"}

    # --- INCOMING (GSM -> Telegram) ---
    if call.direction == "incoming":
        if req.status == "answered":
            # The TG user accepted and the call ran to its end (Dial
            # returned ANSWERED). Fast-forward the intermediate states;
            # no cleanup — the channel is still alive until the
            # dialplan hangs up; the h-exten reports ENDED (above).
            if not registry.fast_forward_bridged(call_id):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot bridge — call is in {call.state.value} state",
                )
            call = registry.get(call_id)
            audit.log(
                EventType.CALL_BRIDGED,
                correlation_id=call_id,
                outcome="ok",
                details={
                    "gsm_channel": call.gsm_channel_id,
                    "bridge_channel": call.bridge_channel_id,
                },
            )
        elif req.status == "no_answer":
            # Telegram ring timed out (Dial returned NOANSWER); the
            # dialplan now takes the voicemail branch.
            if not registry.fallback_to_voicemail(call_id):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot fall back to voicemail — call is in "
                        f"{call.state.value} state"
                    ),
                )
            audit.log(
                EventType.CALL_TELEGRAM_TIMEOUT,
                correlation_id=call_id,
                outcome="voicemail_fallback",
                details={"from": call.caller_number},
            )
            registry.cleanup(call_id)
        elif req.status == "busy":
            # TG user explicitly rejected (bridge returned 486/403).
            if not registry.reject(call_id, reason="telegram_rejected"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot reject — call is in {call.state.value} state",
                )
            audit.log(
                EventType.CALL_REJECTED,
                correlation_id=call_id,
                outcome="telegram_rejected",
                details={"from": call.caller_number},
            )
            registry.cleanup(call_id)
        else:
            # cancelled (the GSM caller hung up, or the TG user hung up
            # while ringing) or failed (the bridge leg itself failed,
            # e.g. bridge down) — close, no voicemail.
            reason = (
                "caller_hangup"
                if req.status == "cancelled"
                else f"bridge_{req.dialstatus or 'failed'}"
            )
            if not registry.hangup(call_id, reason=reason):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot hangup — call is in {call.state.value} state",
                )
            audit.log(
                EventType.CALL_HANGUP,
                correlation_id=call_id,
                outcome=req.status,
                details={
                    "from": call.caller_number,
                    "dialstatus": req.dialstatus,
                },
            )
            registry.cleanup(call_id)

    # --- OUTGOING (Telegram -> GSM) ---
    else:
        if req.status == "answered":
            # Dial(Dongle) returned ANSWERED = the target answered and
            # the call ran. Fast-forward to BRIDGED; no cleanup — the
            # SIP leg is alive until the TG user hangs up (h-exten
            # reports ENDED).
            if not (
                registry.gsm_ringing(call_id)
                and registry.gsm_connected(call_id)
                and registry.bridge_call(call_id)
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot bridge — call is in {call.state.value} state",
                )
            call = registry.get(call_id)
            audit.log(
                EventType.CALL_BRIDGED,
                telegram_user_id=call.telegram_user_id,
                correlation_id=call_id,
                outcome="ok",
                details={"to": call.callee_number},
            )
            await _notify_userbot_call(cfg, call=call, status="answered")
        elif req.status in ("no_answer", "busy", "failed"):
            # The GSM dial failed — a separate localized message to the
            # user (S04.3), then cleanup.
            if req.status == "no_answer":
                ok = registry.gsm_no_answer(call_id)
            elif req.status == "busy":
                ok = registry.gsm_busy(call_id)
            else:
                ok = registry.gsm_error(
                    call_id, reason=req.dialstatus or "network_error"
                )
            if not ok:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot record {req.status} — call is in "
                        f"{call.state.value} state"
                    ),
                )
            audit.log(
                EventType.CALL_HANGUP,
                telegram_user_id=call.telegram_user_id,
                correlation_id=call_id,
                outcome=req.status,
                details={
                    "to": call.callee_number,
                    "dialstatus": req.dialstatus,
                },
            )
            call = registry.get(call_id)
            await _notify_userbot_call(cfg, call=call, status=req.status)
            registry.cleanup(call_id)
        else:
            # cancelled / ended: the TG user hung up while the GSM leg
            # was dialing (channel death) — close. No notification: the
            # user ended the call themselves.
            if not registry.hangup(call_id, reason="telegram_hangup"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot hangup — call is in {call.state.value} state",
                )
            audit.log(
                EventType.CALL_HANGUP,
                telegram_user_id=call.telegram_user_id,
                correlation_id=call_id,
                outcome=req.status,
                details={"to": call.callee_number},
            )
            registry.cleanup(call_id)

    return {"ok": True, "call_id": call_id}


@router.post("/call/{call_id}/accept", response_model=CallStateResponse)
async def call_accept(
    call_id: str,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    ami: AMIClient = Depends(get_ami),
    audit: AuditLogger = Depends(get_audit),
):
    """Accept an incoming call → Telegram accepted → answer GSM → bridge.

    S04.3: The GSM leg was NOT answered while Telegram was ringing.
    Accept transitions: TELEGRAM_RINGING → TELEGRAM_ACCEPTED.
    Then the GSM leg is answered via AMI, and the bridge leg is initiated.
    """
    call = registry.get(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    # Transition to accepted
    ok = registry.accept_incoming(call_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot accept — call is in {call.state.value} state",
        )

    audit.log(
        EventType.CALL_ACCEPTED,
        correlation_id=call_id,
        outcome="ok",
        details={"from": call.caller_number, "direction": call.direction},
    )

    return CallStateResponse(
        call_id=call_id,
        state=call.state.value,
        direction=call.direction,
    )


@router.post("/call/{call_id}/answer-gsm", response_model=CallStateResponse)
async def call_answer_gsm(
    call_id: str,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    ami: AMIClient = Depends(get_ami),
    audit: AuditLogger = Depends(get_audit),
):
    """Answer the GSM leg after Telegram user accepts.

    S04.3: Called after accept. Answers the GSM channel and initiates
    the bridge leg to tg-bridge.
    """
    call = registry.get(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    # Answer the GSM channel
    if call.gsm_channel_id:
        try:
            await ami.answer_channel(call.gsm_channel_id)
        except Exception as e:
            audit.log(
                EventType.CALL_GSM_ANSWERED,
                correlation_id=call_id,
                outcome="error",
                details={"error": str(e)},
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to answer GSM channel: {e}",
            )

    ok = registry.answer_gsm(call_id, gsm_channel_id=call.gsm_channel_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot answer GSM — call is in {call.state.value} state",
        )

    audit.log(
        EventType.CALL_GSM_ANSWERED,
        correlation_id=call_id,
        outcome="ok",
    )

    return CallStateResponse(
        call_id=call_id,
        state=call.state.value,
        direction=call.direction,
    )


@router.post("/call/{call_id}/bridge", response_model=CallStateResponse)
async def call_bridge(
    call_id: str,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    audit: AuditLogger = Depends(get_audit),
):
    """Mark both legs as bridged.

    S04.3: Called when the PJSIP bridge leg connects to the GSM leg.
    """
    call = registry.get(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    ok = registry.bridge_call(call_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot bridge — call is in {call.state.value} state",
        )

    audit.log(
        EventType.CALL_BRIDGED,
        correlation_id=call_id,
        outcome="ok",
        details={
            "gsm_channel": call.gsm_channel_id,
            "bridge_channel": call.bridge_channel_id,
        },
    )

    return CallStateResponse(
        call_id=call_id,
        state=call.state.value,
        direction=call.direction,
    )


@router.post("/call/{call_id}/set-bridge-leg")
async def call_set_bridge_leg(
    call_id: str,
    channel_id: str,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
):
    """Record the bridge channel ID."""
    ok = registry.set_bridge_leg(call_id, channel_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Call not found")
    return {"ok": True, "call_id": call_id, "bridge_channel_id": channel_id}


@router.post("/call/{call_id}/reject", response_model=CallStateResponse)
async def call_reject(
    call_id: str,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    audit: AuditLogger = Depends(get_audit),
):
    """Reject an incoming call → hang up GSM leg, audit the reason."""
    call = registry.get(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    ok = registry.reject(call_id, reason="user_rejected")
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reject — call is in {call.state.value} state",
        )
    audit.log(
        EventType.CALL_REJECTED,
        correlation_id=call_id,
        outcome="user_rejected",
        details={"from": call.caller_number},
    )
    registry.cleanup(call_id)
    return CallStateResponse(
        call_id=call_id,
        state=call.state.value,
        direction=call.direction,
    )


@router.post("/call/{call_id}/hangup", response_model=CallStateResponse)
async def call_hangup(
    call_id: str,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    ami: AMIClient = Depends(get_ami),
    audit: AuditLogger = Depends(get_audit),
):
    """Hang up a call → symmetric hangup, terminate both legs, cleanup.

    S04.3: Either side hanging up terminates both legs. No orphan channels.
    """
    call = registry.get(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    # Symmetric hangup: terminate the other leg if it exists
    channels = call.get_active_channel_ids()
    for channel_id in channels:
        try:
            await ami.hangup_channel(channel_id, reason="BYE")
        except Exception as e:
            audit.log(
                EventType.CALL_HANGUP,
                correlation_id=call_id,
                outcome="partial_hangup",
                details={"error": str(e), "channel": channel_id},
            )

    ok = registry.hangup(call_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot hangup — call is in {call.state.value} state",
        )

    audit.log(
        EventType.CALL_HANGUP,
        correlation_id=call_id,
        outcome="ok",
        details={"channels_terminated": len(channels)},
    )

    registry.cleanup(call_id)
    return CallStateResponse(
        call_id=call_id,
        state=call.state.value,
        direction=call.direction,
    )


@router.post("/call/{call_id}/telegram-ring")
async def call_telegram_ring(
    call_id: str,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    audit: AuditLogger = Depends(get_audit),
):
    """Start Telegram ringing for an incoming call.

    S04.3: After the GSM caller is ringing, we notify Telegram.
    Transition: RINGING → TELEGRAM_RINGING.
    """
    call = registry.get(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    ok = registry.start_telegram_ring(call_id)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot start telegram ring — call is in {call.state.value} state",
        )
    audit.log(
        EventType.CALL_TELEGRAM_RING,
        correlation_id=call_id,
        outcome="ok",
        details={"from": call.caller_number},
    )
    return {"ok": True, "call_id": call_id}


@router.post("/call/{call_id}/set-gsm-channel")
async def call_set_gsm_channel(
    call_id: str,
    channel_id: str,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
):
    """Record the GSM channel ID for this call."""
    with registry._lock:
        call = registry._calls.get(call_id)
        if not call:
            raise HTTPException(status_code=404, detail="Call not found")
        call.gsm_channel_id = channel_id
    return {"ok": True, "call_id": call_id, "gsm_channel_id": channel_id}


@router.post("/call/check-timeouts")
async def call_check_timeouts(
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
    ami: AMIClient = Depends(get_ami),
    cfg: dict = Depends(get_cfg),
    audit: AuditLogger = Depends(get_audit),
):
    """Check for calls that exceeded their timeout window.

    S04.3:
    - Incoming ring timeout -> voicemail fallback. Backstop: the
      dialplan Dial timeout normally handles this; this catches calls
      whose dialplan event was lost.
    - Outgoing Telegram ring timeout -> TELEGRAM_TIMEOUT + user
      notified. The Telegram ring is out-of-band (no dialplan Dial
      enforces it), so THIS driver is the only enforcement. No channel
      exists yet to hang up (the bridge has not INVITEd); a late accept
      later hits a 404 at /call/outgoing/accepted (nocal gate).
    - Bridged calls past max_call_seconds -> hangup both legs via AMI.
      Hanging either leg cascades (Dial's hanguptree / ast_dial_destroy
      tears down the other).
    """
    ring_wait = cfg.get("asterisk", {}).get("ring_wait_seconds", 24)
    max_call = cfg.get("limits", {}).get("max_call_seconds", 1800)
    tg_ring = cfg.get("voice", {}).get("outbound_answer_timeout", 30)

    timed_out = registry.get_timed_out_calls(
        ring_wait, max_call, tg_ring_seconds=tg_ring
    )
    handled: list[dict] = []

    for call in timed_out:
        if call.state in (CallState.RINGING, CallState.TELEGRAM_RINGING):
            registry.fallback_to_voicemail(call.call_id)
            audit.log(
                EventType.CALL_TELEGRAM_TIMEOUT,
                correlation_id=call.call_id,
                outcome="voicemail_fallback",
                details={"from": call.caller_number},
            )
            # The dialplan is self-contained (its CALL_ID is a channel
            # variable; h-exten's ENDED report hits a 404 no-op), so the
            # record is no longer needed — same as the /complete path.
            # Without this every lost-event call leaks one entry forever.
            registry.cleanup(call.call_id)
            handled.append({"call_id": call.call_id, "action": "voicemail"})
        elif call.state in (CallState.MODEM_RESERVED, CallState.TELEGRAM_CALLING):
            registry.telegram_timeout(call.call_id)
            audit.log(
                EventType.CALL_TELEGRAM_TIMEOUT,
                telegram_user_id=call.telegram_user_id,
                correlation_id=call.call_id,
                outcome="no_answer",
                details={"to": call.callee_number},
            )
            await _notify_userbot_call(cfg, call=call, status="no_answer")
            registry.cleanup(call.call_id)
            handled.append({"call_id": call.call_id, "action": "telegram_timeout"})
        elif call.state == CallState.BRIDGED:
            # AMI hangup of each active leg, then close the call in the
            # registry — the state transition does NOT depend on the AMI
            # result: if Asterisk is dead there is no channel left for
            # h-exten to report, and a retry loop would hold the modem
            # reservation forever. If a hangup fails against a LIVE
            # Asterisk, the leg self-heals (user hangup / rtptimeout=60)
            # and the dialplan's h-exten ENDED then hits the terminal
            # state as a 404 no-op.
            for channel_id in call.get_active_channel_ids():
                try:
                    await ami.hangup_channel(channel_id, reason="max_duration")
                except Exception as e:
                    audit.log(
                        EventType.CALL_HANGUP,
                        correlation_id=call.call_id,
                        outcome="partial_hangup",
                        details={"error": str(e), "channel": channel_id},
                    )
            registry.hangup(call.call_id, reason="max_duration_exceeded")
            audit.log(
                EventType.CALL_DURATION_EXPIRED,
                telegram_user_id=call.telegram_user_id,
                correlation_id=call.call_id,
                outcome="max_duration",
            )
            registry.cleanup(call.call_id)
            handled.append({"call_id": call.call_id, "action": "hangup_duration"})

    return {"timed_out": len(handled), "actions": handled}


@router.get("/call/{call_id}", response_model=CallStateResponse)
async def call_state(
    call_id: str,
    registry: CallRegistry = Depends(get_call_registry),
):
    """Get current call state."""
    call = registry.get(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return CallStateResponse(
        call_id=call.call_id,
        state=call.state.value,
        direction=call.direction,
        error=call.error,
    )


@router.get("/calls")
async def list_calls(
    registry: CallRegistry = Depends(get_call_registry),
):
    """List all active calls."""
    return [c.to_dict() for c in registry.list_active()]


@router.get("/health", response_model=HealthResponse)
async def health(
    ami: AMIClient = Depends(get_ami),
    checker: "HealthChecker" = Depends(get_health_checker),
    metrics_collector: "MetricsCollector" = Depends(get_metrics),
):
    """Comprehensive health check + Asterisk reachability + dongle state + metrics.

    S06.2: Uses HealthChecker for component-level status and MetricsCollector
    for aggregated SMS/call counts.
    """
    asterisk_ok = False
    dongle_registered = None

    try:
        status = await ami.get_modem_status()
        asterisk_ok = True
        dongle_registered = status.get("registered", False)
    except (ConnectionError, OSError):
        asterisk_ok = False

    # Update metrics with current component state
    if asterisk_ok is not None:
        metrics_collector.set_modem_registered(dongle_registered)

    # Run comprehensive health checks
    health_status = await checker.check_all()

    return HealthResponse(
        status="ok" if asterisk_ok else "degraded",
        asterisk_reachable=asterisk_ok,
        dongle_registered=dongle_registered,
        timestamp=datetime.now(timezone.utc).isoformat(),
        components=health_status.to_dict(),
        metrics=metrics_collector.get_all(),
    )
