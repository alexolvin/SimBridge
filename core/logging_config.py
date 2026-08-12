"""Structured JSON logging — one line per event, UTC timestamps, correlation IDs.

All timestamps are UTC in storage; local time only at display.
Usage::

    from core.logging_config import setup_logging
    setup_logging(level="INFO", json_format=True)

Correlation IDs are propagated via contextvars so that every log line
within a request carries the same ``correlation_id``.

    from core.logging_config import set_correlation, get_correlation
    set_correlation("abc123")
    logger.info("processing SMS")  # → {"correlation_id": "abc123", ...}
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

# Thread-safe context variable for correlation IDs
_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)


def set_correlation(cid: str) -> None:
    """Set the correlation ID for the current context."""
    _correlation_id.set(cid)


def get_correlation() -> str:
    """Get the current correlation ID (empty string if not set)."""
    return _correlation_id.get()


class JSONFormatter(logging.Formatter):
    """Format log records as single-line JSON with UTC timestamps.

    Output::

        {"ts":"2026-08-12T10:30:00.123456+00:00","level":"INFO","logger":"simbridge.agent","msg":"started","correlation_id":"abc123"}
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Add correlation ID if set
        cid = get_correlation()
        if cid:
            entry["correlation_id"] = cid

        # Add exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = self.formatException(record.exc_info)

        # Add extra fields (key=value pairs passed as extras)
        if hasattr(record, "correlation_id") and record.correlation_id:
            entry["correlation_id"] = record.correlation_id

        for key in ("modem_id", "event", "outcome", "details"):
            if hasattr(record, key):
                val = getattr(record, key)
                if val is not None:
                    entry[key] = val

        return json.dumps(entry, default=str, ensure_ascii=False)


class StructuredAdapter(logging.LoggerAdapter):
    """Logger adapter that injects correlation_id from contextvars into every record."""

    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        extra = kwargs.get("extra", {}) or {}
        extra["correlation_id"] = get_correlation()
        kwargs["extra"] = extra
        return msg, kwargs


def setup_logging(
    level: str = "INFO",
    json_format: bool = True,
    output: Optional[str] = None,
) -> None:
    """Configure root logger with structured JSON or plain-text formatting.

    Parameters
    ----------
    level: logging level string (DEBUG, INFO, WARNING, ERROR)
    json_format: if True, use JSON formatter; if False, use plain text
    output: optional file path for log output (default: stderr)
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear existing handlers to avoid duplicates on re-init
    root.handlers.clear()

    handler = logging.StreamHandler(
        sys.stderr if output is None else open(output, "a")
    )

    if json_format:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    handler.setFormatter(formatter)
    root.addHandler(handler)


def get_logger(name: str) -> logging.LoggerAdapter:
    """Get a structured logger that auto-injects correlation IDs.

    Returns a ``StructuredAdapter`` wrapping a standard logger.
    """
    return StructuredAdapter(logging.getLogger(name), {})
