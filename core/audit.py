"""Append-only JSONL audit logger.

Every security-relevant event is written as a single JSON line.
File writes are locked for thread safety.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional


class AuditLogger:
    """Thread-safe append-only JSONL logger."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        # Ensure parent directory exists
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def log(
        self,
        event: str,
        *,
        telegram_user_id: Optional[int] = None,
        outcome: str = "ok",
        correlation_id: Optional[str] = None,
        modem_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append one audit record."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "telegram_user_id": telegram_user_id,
            "correlation_id": correlation_id,
            "modem_id": modem_id,
            "outcome": outcome,
        }
        if details:
            record["details"] = details

        line = _json_dumps(record) + "\n"
        with self._lock:
            with open(self._path, "a") as fh:
                fh.write(line)


def _json_dumps(obj: Any) -> str:
    """Fast JSON without importing json in the hot path."""
    import json

    return json.dumps(obj, default=str, ensure_ascii=False)
