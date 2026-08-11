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
    """Register an incoming GSM → Telegram call."""
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
    audit: AuditLogger = Depends(get_audit),
):
    """Register an outgoing Telegram → GSM call."""
    if blacklist.contains(req.phone_number):
        raise HTTPException(
            status_code=403,
            detail=SMSErrorType.BLACKLISTED.message,
        )
    try:
        call = registry.create_outgoing(
            callee_number=req.phone_number,
            caller_number=f"user:{req.telegram_user_id or 0}",
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
):
    """Accept an incoming call → bridge GSM leg."""
    call = registry.get(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    ok = registry.transition(call_id, CallState.ACCEPTED)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot accept — call is in {call.state.value} state",
        )
    return CallStateResponse(
        call_id=call_id,
        state=call.state.value,
        direction=call.direction,
    )


@router.post("/call/{call_id}/reject", response_model=CallStateResponse)
async def call_reject(
    call_id: str,
    request: Request,
    registry: CallRegistry = Depends(get_call_registry),
):
    """Reject an incoming call."""
    call = registry.get(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    ok = registry.transition(call_id, CallState.REJECTED)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot reject — call is in {call.state.value} state",
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
):
    """Hang up a call → cleanup both legs."""
    call = registry.get(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    ok = registry.transition(call_id, CallState.HANGUP)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot hangup — call is in {call.state.value} state",
        )
    registry.cleanup(call_id)
    return CallStateResponse(
        call_id=call_id,
        state=call.state.value,
        direction=call.direction,
    )


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
