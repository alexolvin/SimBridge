"""Agent API routes — SMS, modem, health endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field

from core.audit import AuditLogger
from core.events import EventType
from core.ratelimit import RateLimiter
from agent.ami_client import AMIClient
from agent.deps import require_auth, get_ami, get_cfg, get_audit, get_sms_limiter

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


class SMSResponse(BaseModel):
    ok: bool
    correlation_id: str
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
):
    """Send outgoing SMS via Asterisk + chan_dongle.

    **Security:** SMS text is passed as a parameter to the AMI client,
    never interpolated into a shell command (Rule 1).

    **Auth:** Requires valid bearer token + allowed peer IP (checked by
    router-level Depends(require_auth)).
    """
    correlation_id = req.correlation_id or uuid.uuid4().hex

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

    # Audit: request received
    audit.log(
        EventType.SMS_SEND_REQUESTED,
        telegram_user_id=req.telegram_user_id,
        outcome="submitted",
        correlation_id=correlation_id,
        details={"to": req.to},
    )

    try:
        await ami.send_sms(req.to, req.text)
    except ConnectionError:
        raise HTTPException(status_code=503, detail="Asterisk AMI unreachable")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Audit: submitted to modem
    audit.log(
        EventType.SMS_SUBMITTED,
        telegram_user_id=req.telegram_user_id,
        outcome="ok",
        correlation_id=correlation_id,
        modem_id="gsm",
        details={"to": req.to},
    )

    return SMSResponse(
        ok=True,
        correlation_id=correlation_id,
        message=f"SMS submitted to {req.to}",
    )


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
