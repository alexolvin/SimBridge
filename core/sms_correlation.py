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

Persistence: when constructed with *log_path*, every state change is
appended to that file as a full JSON record line. On restart the file
is reloaded (last line per sms_id wins), so delivery correlation
survives agent restarts.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
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
    """Thread-safe store for SMS correlation records.

    Records live in memory; when *log_path* is given, every state
    change is also appended to that JSONL file, and the file is
    reloaded on construction — so correlation survives restarts.
    Without *log_path* the store is memory-only (tests).
    """

    def __init__(self, log_path: Optional[str] = None) -> None:
        self._records: Dict[str, SMSRecord] = {}
        self._lock = threading.Lock()
        self._log_path = log_path
        if log_path:
            self._load(log_path)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self, path: str) -> None:
        """Load records from a JSONL file (last line per sms_id wins)."""
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning("Cannot read SMS correlation log %s: %s", path, e)
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                rec = SMSRecord(**data)
            except (ValueError, TypeError):
                logger.warning(
                    "Skipping unparseable SMS correlation line: %r", line[:120]
                )
                continue
            with self._lock:
                self._records[rec.sms_id] = rec
        logger.info(
            "Loaded %d SMS correlation records from %s", len(self._records), path
        )

    def _persist(self, rec: SMSRecord) -> None:
        """Append the full record as one JSON line (best-effort)."""
        if not self._log_path:
            return
        try:
            line = json.dumps(asdict(rec), ensure_ascii=False) + "\n"
            fd = os.open(self._log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as e:
            logger.warning(
                "Cannot append SMS correlation log %s: %s", self._log_path, e
            )

    # ------------------------------------------------------------------
    # Record lifecycle
    # ------------------------------------------------------------------

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
            self._persist(record)
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
            self._persist(rec)
            return True

    def mark_delivered(self, sms_id: str) -> bool:
        """Mark SMS as delivered."""
        with self._lock:
            rec = self._records.get(sms_id)
            if not rec:
                return False
            rec.delivery_status = "delivered"
            rec.delivered_at = datetime.now(timezone.utc).isoformat()
            self._persist(rec)
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
            self._persist(rec)
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

    # ------------------------------------------------------------------
    # Delivery report matching
    # ------------------------------------------------------------------

    def match_report(self, modem_id: str, report_text: str) -> Optional[SMSRecord]:
        """Match a carrier delivery report to a pending SMS record.

        Candidates are records on *modem_id* that were submitted but not
        yet resolved. Carrier reports usually name the recipient number:
        if a candidate's number (E.164 or 8-prefix digit form) appears
        in the report text, that candidate wins. Otherwise the newest
        pending candidate is returned, so a report without a
        recognizable number still resolves.
        """
        with self._lock:
            candidates = [
                rec
                for rec in self._records.values()
                if (
                    rec.submit_status == "submitted"
                    and rec.delivery_status == "pending"
                    and rec.modem_id == modem_id
                )
            ]
            if not candidates:
                return None

            digits = "".join(ch for ch in report_text if ch.isdigit())
            hinted = [
                rec
                for rec in candidates
                if any(v and v in digits for v in self._phone_digit_variants(rec.phone_number))
            ]
            pool = hinted or candidates
            pool.sort(key=lambda r: r.submitted_at or r.created_at, reverse=True)
            return pool[0]

    @staticmethod
    def _phone_digit_variants(phone_number: str) -> List[str]:
        """Digit forms of a phone number that may appear in a report.

        E.164 "+79261234555" -> ["79261234555", "89261234555"];
        Russian carrier reports usually carry the 7- or 8-prefix form.
        """
        digits = "".join(ch for ch in phone_number if ch.isdigit())
        if not digits:
            return []
        variants = [digits]
        if digits.startswith("7"):
            variants.append("8" + digits[1:])
        return variants
