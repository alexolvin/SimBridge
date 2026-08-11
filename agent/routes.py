"""Agent API routes — SMS, modem, health, blacklist endpoints.

Integrates contacts, blacklist, sms_correlation, and error surfaces (Stage 02).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field

from core.audit import AuditLogger
from core.events import EventType
from core.ratelimit import RateLimiter
from core.blacklist import BlacklistManager
from core.sms_correlation import SMSCorrelationStore
from core.errors import SMSErrorType
from core.acl import ACLManager
from agent.ami_client import AMIClient
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
)
from core.call_control import CallRegistry, CallState, InvalidTransition, ModemBusyError

router = APIRouter()
# Apply auth + IP allowlist + replay protection to all /v1 routes
router.dependencies.append(Depends(require_auth))


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


class OutgoingCallRequest(BaseModel):
    phone_number: str = Field(..., description="Destination (E.164)")
    telegram_user_id: Optional[int] = Field(None, description="Sender for ACL")


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
):
    """Send outgoing SMS via Asterisk + chan_dongle.

    **Security:** SMS text is passed as a parameter to the AMI client,
    never interpolated into a shell command (Rule 1).

    **Auth:** Requires valid bearer token + allowed peer IP (checked by
    router-level Depends(require_auth)).

    **S02:** Checks blacklist, creates correlation record, returns sms_id.
    """
    correlation_id = req.correlation_id or uuid.uuid4().hex

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
        cfg = get_cfg(request)
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
    except ConnectionError:
        sms_store.mark_failed(
            record.sms_id,
            error=SMSErrorType.MODEM_UNAVAILABLE.message,
            submit_failed=True,
        )
        raise HTTPException(status_code=503, detail=SMSErrorType.MODEM_UNAVAILABLE.message)
    except Exception as e:
        err_msg = str(e)
        sms_store.mark_failed(
            record.sms_id,
            error=err_msg,
            submit_failed=True,
        )
        raise HTTPException(status_code=500, detail=err_msg)

    # Audit: submitted to modem
    audit.log(
        EventType.SMS_SUBMITTED,
        telegram_user_id=req.telegram_user_id,
        outcome="ok",
        correlation_id=correlation_id,
        modem_id="gsm",
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


@router.get("/modems", response_model=list[ModemInfo])
async def get_modems(
    ami: AMIClient = Depends(get_ami),
):
    """Query modem registration and signal state."""
    try:
        status = await ami.get_modem_status()
        return [ModemInfo(
            device=status.get("device", "gsm"),
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
    """
    call = registry.create_incoming(
        caller_number=req.phone_number,
        caller_name=req.contact_name,
    )
    audit.log(
        EventType.CALL_INCOMING,
        outcome="ok",
        correlation_id=call.call_id,
        details={"from": req.phone_number},
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
):
    """Register an outgoing Telegram → GSM call.

    S04.3: ACL is checked BEFORE any call session is created.
    Never call the user first and authorize afterwards.
    """
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
    except ModemBusyError:
        raise HTTPException(status_code=503, detail="Modem busy — another call in progress")
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
    cfg: dict = Depends(get_cfg),
    audit: AuditLogger = Depends(get_audit),
):
    """Check for calls that have exceeded ring_wait or max_call_seconds.

    S04.3: Fallback to voicemail on ring timeout.
    """
    ring_wait = cfg.get("asterisk", {}).get("ring_wait_seconds", 24)
    max_call = cfg.get("limits", {}).get("max_call_seconds", 1800)

    timed_out = registry.get_timed_out_calls(ring_wait, max_call)
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
            handled.append({"call_id": call.call_id, "action": "voicemail"})
        elif call.state == CallState.BRIDGED:
            registry.hangup(call.call_id, reason="max_duration_exceeded")
            audit.log(
                EventType.CALL_DURATION_EXPIRED,
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
):
    """Liveness check + Asterisk reachability + dongle state."""
    asterisk_ok = False
    dongle_registered = None

    try:
        status = await ami.get_modem_status()
        asterisk_ok = True
        dongle_registered = status.get("registered", False)
    except (ConnectionError, OSError):
        asterisk_ok = False

    return HealthResponse(
        status="ok" if asterisk_ok else "degraded",
        asterisk_reachable=asterisk_ok,
        dongle_registered=dongle_registered,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
