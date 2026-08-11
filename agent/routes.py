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
)

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
