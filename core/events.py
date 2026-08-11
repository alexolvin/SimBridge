"""Shared event types and schemas used across all SimBridge components.

Every event carries correlation_id for tracing. AuditLog records security-relevant
events in append-only JSONL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class EventType(str, Enum):
    """Security-relevant event types for the audit log."""

    USER_DENIED = "USER_DENIED"
    SMS_SEND_REQUESTED = "SMS_SEND_REQUESTED"
    SMS_SUBMITTED = "SMS_SUBMITTED"
    CALL_REQUESTED = "CALL_REQUESTED"
    BLACKLIST_CHANGED = "BLACKLIST_CHANGED"
    CONFIG_RELOADED = "CONFIG_RELOADED"


@dataclass
class AuditLog:
    """Single audit record. Written as one JSON line to the audit log file."""

    event: EventType
    telegram_user_id: Optional[int] = None
    outcome: str = "ok"
    correlation_id: Optional[str] = None
    modem_id: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(init=False)

    def __post_init__(self) -> None:
        # All timestamps UTC in storage, converted only for display
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_json(self) -> str:
        import json

        return json.dumps(asdict(self), default=str, ensure_ascii=False)


@dataclass
class SMSEvent:
    """Incoming SMS event from Asterisk to the userbot."""

    phone_number: str
    text: str
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    modem_id: str = "gsm"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class ModemState:
    """Modem registration and signal state."""

    device: str
    registered: bool
    signal_percent: Optional[int] = None
    imei_suffix: Optional[str] = None
    operator: Optional[str] = None
