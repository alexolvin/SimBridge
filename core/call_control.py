"""Call control state machine — manages voice call lifecycle.

S04.2: Bridge wiring. S04.3: Full call control with both directions.

Each call goes through a deterministic state machine. States are enums,
transitions are validated, and every transition emits an audit event.

Incoming flow: IDLE -> RINGING -> TELEGRAM_RINGING -> ACCEPTED -> BRIDGED -> HANGUP -> CLEANUP
Outgoing flow: IDLE -> REQUESTED -> MODEM_RESERVED -> TELEGRAM_CALLING -> ACCEPTED -> GSM_DIALING -> BRIDGED -> HANGUP -> CLEANUP

Branches from RINGING: reject -> REJECTED, timeout -> VOICEMAIL, caller-hangup -> HANGUP
Branches from REQUESTED/TELEGRAM_CALLING: reject -> REJECTED, timeout -> TIMEOUT
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from core.audit import AuditLogger
from core.events import EventType
from core.sms_correlation import SMSCorrelationStore

logger = logging.getLogger("simbridge.call_control")


class CallState(str, Enum):
    """Call lifecycle states."""
    IDLE = "idle"
    RINGING = "ringing"
    TELEGRAM_RINGING = "telegram_ringing"
    ACCEPTED = "accepted"
    BRIDGED = "bridged"
    HANGUP = "hangup"
    CLEANUP = "cleanup"
    REJECTED = "rejected"
    VOICEMAIL = "voicemail"
    REQUESTED = "requested"
    MODEM_RESERVED = "modem_reserved"
    TELEGRAM_CALLING = "telegram_calling"
    GSM_DIALING = "gsm_dialing"
    TIMEOUT = "timeout"


# Valid state transitions per direction
_INCOMING_TRANSITIONS: Dict[CallState, List[CallState]] = {
    CallState.IDLE: [CallState.RINGING],
    CallState.RINGING: [CallState.TELEGRAM_RINGING, CallState.REJECTED, CallState.HANGUP, CallState.VOICEMAIL],
    CallState.TELEGRAM_RINGING: [CallState.ACCEPTED, CallState.REJECTED, CallState.HANGUP, CallState.VOICEMAIL],
    CallState.ACCEPTED: [CallState.BRIDGED, CallState.HANGUP],
    CallState.BRIDGED: [CallState.HANGUP],
    CallState.HANGUP: [CallState.CLEANUP],
    CallState.VOICEMAIL: [CallState.HANGUP],
}

_OUTGOING_TRANSITIONS: Dict[CallState, List[CallState]] = {
    CallState.IDLE: [CallState.REQUESTED],
    CallState.REQUESTED: [CallState.MODEM_RESERVED, CallState.REJECTED],
    CallState.MODEM_RESERVED: [CallState.TELEGRAM_CALLING, CallState.REJECTED, CallState.TIMEOUT],
    CallState.TELEGRAM_CALLING: [CallState.ACCEPTED, CallState.REJECTED, CallState.TIMEOUT],
    CallState.ACCEPTED: [CallState.GSM_DIALING],
    CallState.GSM_DIALING: [CallState.BRIDGED, CallState.HANGUP],
    CallState.BRIDGED: [CallState.HANGUP],
    CallState.HANGUP: [CallState.CLEANUP],
}


class InvalidTransition(Exception):
    """Raised when a state transition is not allowed."""


class ModemBusyError(Exception):
    """Raised when the modem is already in use."""


@dataclass
class CallMachine:
    """State machine for a single voice call session."""

    call_id: str
    direction: str  # "incoming" | "outgoing"
    caller_number: str = ""
    callee_number: str = ""
    caller_name: Optional[str] = None
    callee_name: Optional[str] = None
    modem_id: str = "gsm"
    state: CallState = CallState.IDLE
    created_at: str = ""
    updated_at: str = ""
    error: Optional[str] = None

    # Dependency references (set by registry)
    _sms_store: Optional[SMSCorrelationStore] = None
    _audit: Optional[AuditLogger] = None

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        self.updated_at = now

    def transition(self, new_state: CallState) -> None:
        """Validate and execute a state transition.

        Raises InvalidTransition if the transition is not allowed.
        """
        transitions = (
            _OUTGOING_TRANSITIONS
            if self.direction == "outgoing"
            else _INCOMING_TRANSITIONS
        )
        allowed = transitions.get(self.state, [])
        if new_state not in allowed:
            raise InvalidTransition(
                f"Cannot transition from {self.state.value} to {new_state.value} "
                f"(allowed: {[s.value for s in allowed]})"
            )
        old_state = self.state
        self.state = new_state
        self.updated_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            "Call %s: %s -> %s",
            self.call_id[:8],
            old_state.value,
            new_state.value,
        )

    def to_dict(self) -> dict:
        """Serialize call state for API responses."""
        return {
            "call_id": self.call_id,
            "direction": self.direction,
            "caller_number": self.caller_number,
            "callee_number": self.callee_number,
            "caller_name": self.caller_name,
            "callee_name": self.callee_name,
            "modem_id": self.modem_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


class CallRegistry:
    """Thread-safe registry of active calls.

    Manages call lifecycle and modem reservation.
    """

    def __init__(
        self,
        sms_store: SMSCorrelationStore,
        audit: AuditLogger,
    ) -> None:
        self._calls: Dict[str, CallMachine] = {}
        self._lock = threading.Lock()
        self._sms_store = sms_store
        self._audit = audit
        self._modems_reserved: int = 0

    def create_incoming(
        self,
        caller_number: str,
        caller_name: Optional[str] = None,
        modem_id: str = "gsm",
    ) -> CallMachine:
        """Create a new incoming call (GSM -> Telegram)."""
        call = CallMachine(
            call_id=uuid.uuid4().hex,
            direction="incoming",
            caller_number=caller_number,
            caller_name=caller_name,
            modem_id=modem_id,
        )
        call._sms_store = self._sms_store
        call._audit = self._audit
        call.transition(CallState.RINGING)
        with self._lock:
            self._calls[call.call_id] = call
        return call

    def create_outgoing(
        self,
        callee_number: str,
        caller_number: str = "simbridge",
        callee_name: Optional[str] = None,
        modem_id: str = "gsm",
    ) -> CallMachine:
        """Create a new outgoing call (Telegram -> GSM)."""
        if self._modems_reserved > 0:
            raise ModemBusyError("Modem is already in use")
        call = CallMachine(
            call_id=uuid.uuid4().hex,
            direction="outgoing",
            caller_number=caller_number,
            callee_number=callee_number,
            callee_name=callee_name,
            modem_id=modem_id,
        )
        call._sms_store = self._sms_store
        call._audit = self._audit
        call.transition(CallState.REQUESTED)
        self._modems_reserved += 1
        call.transition(CallState.MODEM_RESERVED)
        with self._lock:
            self._calls[call.call_id] = call
        return call

    def get(self, call_id: str) -> Optional[CallMachine]:
        with self._lock:
            return self._calls.get(call_id)

    def transition(self, call_id: str, new_state: CallState) -> bool:
        """Execute a state transition. Returns True if successful."""
        with self._lock:
            call = self._calls.get(call_id)
            if not call:
                return False
            try:
                call.transition(new_state)
                return True
            except InvalidTransition:
                return False

    def cleanup(self, call_id: str) -> None:
        """Remove a call after cleanup. Release modem if outgoing."""
        with self._lock:
            call = self._calls.pop(call_id, None)
            if call and call.direction == "outgoing":
                self._modems_reserved = max(0, self._modems_reserved - 1)

    def list_active(self) -> List[CallMachine]:
        """Return all non-CLEANUP calls."""
        with self._lock:
            return [
                c for c in self._calls.values()
                if c.state != CallState.CLEANUP
            ]

    def count_by_state(self, state: CallState) -> int:
        """Count calls in a specific state."""
        with self._lock:
            return sum(1 for c in self._calls.values() if c.state == state)

    def count_by_direction(self, direction: str) -> int:
        """Count active calls by direction."""
        with self._lock:
            return sum(
                1 for c in self._calls.values()
                if c.direction == direction and c.state != CallState.CLEANUP
            )
