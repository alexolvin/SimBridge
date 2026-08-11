"""SMS correlation tracking — links outgoing SMS to delivery reports.

Every outgoing SMS gets a unique `sms_id` that ties together:
- telegram_user_id (sender)
- telegram_message_id (the /sms command message)
- sms_id (unique identifier)
- phone_number (recipient)
- modem_id
- submit_status / delivery_status
- timestamps

Delivery reports resolve by sms_id, not by searching message text
(fragile and breaks with concurrent sends).
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("simbridge.sms_correlation")


@dataclass
class SMSRecord:
    """One outgoing SMS lifecycle record."""

    sms_id: str
    telegram_user_id: int
    telegram_message_id: Optional[int] = None
    phone_number: str = ""
    modem_id: str = "gsm"
    text: str = ""
    submit_status: str = "pending"  # pending, submitted, failed
    delivery_status: str = "pending"  # pending, delivered, failed, expired
    submitted_at: Optional[str] = None
    delivered_at: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()


class SMSCorrelationStore:
    """In-memory store for SMS correlation records.

    Thread-safe. Records are kept in memory and optionally persisted
    to JSONL for debugging.
    """

    def __init__(self, log_path: Optional[str] = None) -> None:
        self._records: Dict[str, SMSRecord] = {}
        self._lock = threading.Lock()
        self._log_path = log_path

    def create(
        self,
        telegram_user_id: int,
        phone_number: str,
        text: str,
        telegram_message_id: Optional[int] = None,
        modem_id: str = "gsm",
    ) -> SMSRecord:
        """Create a new SMS correlation record."""
        record = SMSRecord(
            sms_id=uuid.uuid4().hex,
            telegram_user_id=telegram_user_id,
            telegram_message_id=telegram_message_id,
            phone_number=phone_number,
            modem_id=modem_id,
            text=text,
        )
        with self._lock:
            self._records[record.sms_id] = record
        logger.info(
            "Created SMS record: %s → %s (user=%s)",
            record.sms_id[:8],
            record.phone_number,
            record.telegram_user_id,
        )
        return record

    def get(self, sms_id: str) -> Optional[SMSRecord]:
        with self._lock:
            return self._records.get(sms_id)

    def mark_submitted(self, sms_id: str) -> bool:
        """Mark SMS as submitted to the modem."""
        with self._lock:
            rec = self._records.get(sms_id)
            if not rec:
                return False
            rec.submit_status = "submitted"
            rec.submitted_at = datetime.now(timezone.utc).isoformat()
            return True

    def mark_delivered(self, sms_id: str) -> bool:
        """Mark SMS as delivered."""
        with self._lock:
            rec = self._records.get(sms_id)
            if not rec:
                return False
            rec.delivery_status = "delivered"
            rec.delivered_at = datetime.now(timezone.utc).isoformat()
            return True

    def mark_failed(
        self, sms_id: str, error: str, submit_failed: bool = False
    ) -> bool:
        """Mark SMS as failed.

        Args:
            sms_id: The SMS to mark.
            error: Human-readable error message.
            submit_failed: If True, marks submit_status as failed.
                Otherwise, marks delivery_status as failed.
        """
        with self._lock:
            rec = self._records.get(sms_id)
            if not rec:
                return False
            rec.error_message = error
            if submit_failed:
                rec.submit_status = "failed"
            else:
                rec.delivery_status = "failed"
            return True

    def find_by_telegram_message(
        self, telegram_user_id: int, message_id: int
    ) -> Optional[SMSRecord]:
        """Find the SMS record for a reply to a specific Telegram message."""
        with self._lock:
            for rec in reversed(self._records.values()):
                if (
                    rec.telegram_user_id == telegram_user_id
                    and rec.telegram_message_id == message_id
                ):
                    return rec
        return None

    def recent(
        self, telegram_user_id: int, limit: int = 10
    ) -> List[SMSRecord]:
        """Return recent records for a user, newest first."""
        with self._lock:
            user_records = [
                rec
                for rec in self._records.values()
                if rec.telegram_user_id == telegram_user_id
            ]
            user_records.sort(key=lambda r: r.created_at, reverse=True)
            return user_records[:limit]
