"""Call control state machine — manages voice call lifecycle.

S04.2: Bridge wiring. S04.3: Full call control with both directions.

Each call goes through a deterministic state machine. States are enums,
transitions are validated, and every transition emits an audit event.

Incoming flow (GSM → Telegram):
    IDLE → RINGING → TELEGRAM_RINGING → TELEGRAM_ACCEPTED → GSM_ANSWERED → BRIDGED → HANGUP → CLEANUP
    Branches from RINGING:
      - GSM caller hangs up → HANGUP
      - Telegram user rejects → REJECTED
      - Timeout (ring_wait_seconds) → VOICEMAIL

Outgoing flow (Telegram → GSM):
    IDLE → REQUESTED → ACL_CHECKED → MODEM_RESERVED → TELEGRAM_CALLING
      → USER_ACCEPTED → GSM_DIALING → GSM_RINGING → CONNECTED → BRIDGED → HANGUP → CLEANUP
    Branches:
      - ACL denied → ACL_DENIED (terminal)
      - Telegram user doesn't answer → TELEGRAM_TIMEOUT (terminal)
      - GSM busy → GSM_BUSY (terminal)
      - GSM no answer → GSM_NO_ANSWER (terminal)
      - GSM network error → GSM_ERROR (terminal)

Bridge legs:
    gsm_channel_id — Asterisk channel name for the GSM leg (chan_dongle)
    bridge_channel_id — Asterisk channel name for the bridge leg (PJSIP/tg-bridge)

Symmetric hangup:
    Either side hanging up triggers HANGUP → CLEANUP, both legs terminated.
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
from core.modem import ModemPool, ModemInfo
from core.sms_correlation import SMSCorrelationStore

logger = logging.getLogger("simbridge.call_control")


class CallState(str, Enum):
    """Call lifecycle states."""

    # -- Shared terminal states --
    IDLE = "idle"
    HANGUP = "hangup"
    CLEANUP = "cleanup"
    REJECTED = "rejected"

    # -- Incoming path (GSM → Telegram) --
    RINGING = "ringing"
    TELEGRAM_RINGING = "telegram_ringing"
    TELEGRAM_ACCEPTED = "telegram_accepted"
    GSM_ANSWERED = "gsm_answered"
    VOICEMAIL = "voicemail"

    # -- Outgoing path (Telegram → GSM) --
    REQUESTED = "requested"
    ACL_CHECKED = "acl_checked"
    ACL_DENIED = "acl_denied"
    MODEM_RESERVED = "modem_reserved"
    TELEGRAM_CALLING = "telegram_calling"
    USER_ACCEPTED = "user_accepted"
    GSM_DIALING = "gsm_dialing"
    GSM_RINGING = "gsm_ringing"
    GSM_BUSY = "gsm_busy"
    GSM_NO_ANSWER = "gsm_no_answer"
    GSM_ERROR = "gsm_error"
    CONNECTED = "connected"
    TELEGRAM_TIMEOUT = "telegram_timeout"

    # -- Both directions --
    BRIDGED = "bridged"


# Valid state transitions per direction
_INCOMING_TRANSITIONS: Dict[CallState, List[CallState]] = {
    CallState.IDLE: [CallState.RINGING],
    CallState.RINGING: [
        CallState.TELEGRAM_RINGING,
        CallState.REJECTED,
        CallState.HANGUP,
        CallState.VOICEMAIL,
    ],
    CallState.TELEGRAM_RINGING: [
        CallState.TELEGRAM_ACCEPTED,
        CallState.REJECTED,
        CallState.HANGUP,
        CallState.VOICEMAIL,
    ],
    CallState.TELEGRAM_ACCEPTED: [CallState.GSM_ANSWERED, CallState.HANGUP],
    CallState.GSM_ANSWERED: [CallState.BRIDGED, CallState.HANGUP],
    CallState.BRIDGED: [CallState.HANGUP],
    CallState.HANGUP: [CallState.CLEANUP],
    CallState.VOICEMAIL: [CallState.HANGUP],
}

_OUTGOING_TRANSITIONS: Dict[CallState, List[CallState]] = {
    CallState.IDLE: [CallState.REQUESTED],
    CallState.REQUESTED: [CallState.ACL_CHECKED, CallState.REJECTED],
    CallState.ACL_CHECKED: [CallState.ACL_DENIED, CallState.MODEM_RESERVED],
    CallState.MODEM_RESERVED: [
        CallState.TELEGRAM_CALLING,
        CallState.REJECTED,
        CallState.TELEGRAM_TIMEOUT,
    ],
    CallState.TELEGRAM_CALLING: [
        CallState.USER_ACCEPTED,
        CallState.REJECTED,
        CallState.TELEGRAM_TIMEOUT,
    ],
    CallState.USER_ACCEPTED: [CallState.GSM_DIALING],
    CallState.GSM_DIALING: [
        CallState.GSM_RINGING,
        CallState.GSM_BUSY,
        CallState.GSM_NO_ANSWER,
        CallState.GSM_ERROR,
    ],
    CallState.GSM_RINGING: [CallState.CONNECTED, CallState.GSM_NO_ANSWER, CallState.GSM_ERROR],
    CallState.CONNECTED: [CallState.BRIDGED, CallState.HANGUP],
    CallState.BRIDGED: [CallState.HANGUP],
    CallState.HANGUP: [CallState.CLEANUP],
}

# Terminal states — no further transitions expected
_TERMINAL_STATES = {
    CallState.CLEANUP,
    CallState.REJECTED,
    CallState.HANGUP,
    CallState.VOICEMAIL,
    CallState.ACL_DENIED,
    CallState.TELEGRAM_TIMEOUT,
    CallState.GSM_BUSY,
    CallState.GSM_NO_ANSWER,
    CallState.GSM_ERROR,
}


class InvalidTransition(Exception):
    """Raised when a state transition is not allowed."""


class ModemBusyError(Exception):
    """Raised when the modem is already in use."""


class ACLDeniedError(Exception):
    """Raised when a call request fails ACL check."""


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

    # Bridge leg tracking (S04.3)
    gsm_channel_id: Optional[str] = None
    bridge_channel_id: Optional[str] = None

    # Telegram call session tracking
    telegram_user_id: Optional[int] = None
    telegram_call_id: Optional[str] = None

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

    @property
    def is_terminal(self) -> bool:
        """Return True if the call is in a terminal state."""
        return self.state in _TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        """Return True if the call has active legs (not in a terminal state)."""
        return not self.is_terminal

    def check_duration_exceeded(self, max_seconds: int) -> bool:
        """Check if the call duration has exceeded max_seconds.

        Returns True if the call should be hung up due to duration limit.
        """
        try:
            created = datetime.fromisoformat(self.created_at)
            elapsed = (datetime.now(timezone.utc) - created).total_seconds()
            return elapsed > max_seconds
        except (ValueError, TypeError):
            return False

    def get_active_channel_ids(self) -> List[str]:
        """Return list of active Asterisk channel IDs for this call."""
        channels = []
        if self.gsm_channel_id:
            channels.append(self.gsm_channel_id)
        if self.bridge_channel_id:
            channels.append(self.bridge_channel_id)
        return channels

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
            "gsm_channel_id": self.gsm_channel_id,
            "bridge_channel_id": self.bridge_channel_id,
            "telegram_user_id": self.telegram_user_id,
            "telegram_call_id": self.telegram_call_id,
            "is_terminal": self.is_terminal,
        }


class CallRegistry:
    """Thread-safe registry of active calls.

    Manages call lifecycle, modem reservation, and timeout handling.
    S05.1: Uses ModemPool for modem selection and provenance tracking.
    """

    def __init__(
        self,
        sms_store: SMSCorrelationStore,
        audit: AuditLogger,
        modem_pool: Optional[ModemPool] = None,
    ) -> None:
        self._calls: Dict[str, CallMachine] = {}
        self._lock = threading.Lock()
        self._sms_store = sms_store
        self._audit = audit
        self._modems_reserved: int = 0
        # S05.1: Modem pool for multi-modem support
        self._modem_pool = modem_pool

    @property
    def modem_pool(self) -> Optional[ModemPool]:
        return self._modem_pool

    def create_incoming(
        self,
        caller_number: str,
        caller_name: Optional[str] = None,
        modem_id: str = "gsm",
        gsm_channel_id: Optional[str] = None,
    ) -> CallMachine:
        """Create a new incoming call (GSM -> Telegram).

        Starts in RINGING state. GSM channel is NOT answered yet —
        the caller hears real ringback while we ring Telegram.
        """
        call = CallMachine(
            call_id=uuid.uuid4().hex,
            direction="incoming",
            caller_number=caller_number,
            caller_name=caller_name,
            modem_id=modem_id,
            gsm_channel_id=gsm_channel_id,
        )
        call._sms_store = self._sms_store
        call._audit = self._audit
        call.transition(CallState.RINGING)
        with self._lock:
            self._calls[call.call_id] = call
        logger.info(
            "Incoming call %s from %s (%s) on %s",
            call.call_id[:8],
            caller_number,
            caller_name or "unknown",
            modem_id,
        )
        return call

    def create_outgoing(
        self,
        callee_number: str,
        caller_number: str = "simbridge",
        callee_name: Optional[str] = None,
        modem_id: str = "gsm",
        telegram_user_id: Optional[int] = None,
    ) -> CallMachine:
        """Create a new outgoing call (Telegram -> GSM).

        Starts in REQUESTED state. ACL check happens before this
        (in the route handler). Modem is selected via pool (S05.1)
        or reserved directly (backward compat for single-modem).
        """
        # S05.1: Try pool-based selection first
        selected_modem_id = modem_id
        if self._modem_pool:
            chosen = self._modem_pool.select_for_call(destination=callee_number)
            if chosen is None:
                raise ModemBusyError("All modems busy — no modem available for this call")
            selected_modem_id = chosen.modem_id
        else:
            # Backward compat: direct modem reservation
            if self._modems_reserved > 0:
                raise ModemBusyError("Modem is already in use")

        call = CallMachine(
            call_id=uuid.uuid4().hex,
            direction="outgoing",
            caller_number=caller_number,
            callee_number=callee_number,
            callee_name=callee_name,
            modem_id=selected_modem_id,
            telegram_user_id=telegram_user_id,
        )
        call._sms_store = self._sms_store
        call._audit = self._audit
        call.transition(CallState.REQUESTED)
        # ACL check happens before this (in the route handler).
        # We transition through ACL_CHECKED to record that the check passed.
        call.transition(CallState.ACL_CHECKED)
        if not self._modem_pool:
            self._modems_reserved += 1
        call.transition(CallState.MODEM_RESERVED)
        with self._lock:
            self._calls[call.call_id] = call
        logger.info(
            "Outgoing call %s to %s (user %s) on %s",
            call.call_id[:8],
            callee_number,
            telegram_user_id or "unknown",
            selected_modem_id,
        )
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
                # S05.1: Release via pool if available
                if self._modem_pool:
                    self._modem_pool.release(call.modem_id)
                else:
                    self._modems_reserved = max(0, self._modems_reserved - 1)
            if call:
                logger.info("Cleaned up call %s (was %s)", call.call_id[:8], call.state.value)

    def list_active(self) -> List[CallMachine]:
        """Return all non-terminal calls."""
        with self._lock:
            return [c for c in self._calls.values() if c.is_active]

    def list_all(self) -> List[CallMachine]:
        """Return all calls including terminal ones."""
        with self._lock:
            return list(self._calls.values())

    def count_by_state(self, state: CallState) -> int:
        """Count calls in a specific state."""
        with self._lock:
            return sum(1 for c in self._calls.values() if c.state == state)

    def count_by_direction(self, direction: str) -> int:
        """Count active calls by direction."""
        with self._lock:
            return sum(
                1 for c in self._calls.values()
                if c.direction == direction and c.is_active
            )

    # -----------------------------------------------------------------------
    # Higher-level call orchestration (S04.3)
    # -----------------------------------------------------------------------

    def start_telegram_ring(self, call_id: str) -> bool:
        """Transition from RINGING → TELEGRAM_RINGING.

        Called after sending the Telegram notification to the user.
        """
        return self.transition(call_id, CallState.TELEGRAM_RINGING)

    def accept_incoming(self, call_id: str) -> bool:
        """Transition from TELEGRAM_RINGING → TELEGRAM_ACCEPTED.

        Called when the Telegram user accepts the incoming call.
        Next step: answer the GSM leg.
        """
        return self.transition(call_id, CallState.TELEGRAM_ACCEPTED)

    def answer_gsm(self, call_id: str, gsm_channel_id: Optional[str] = None) -> bool:
        """Transition from TELEGRAM_ACCEPTED → GSM_ANSWERED.

        Called after answering the GSM leg via AMI.
        """
        ok = self.transition(call_id, CallState.GSM_ANSWERED)
        if ok and gsm_channel_id:
            call = self.get(call_id)
            if call:
                call.gsm_channel_id = gsm_channel_id
        return ok

    def set_bridge_leg(self, call_id: str, channel_id: str) -> bool:
        """Record the bridge channel ID.

        Called when the PJSIP bridge leg is established.
        """
        with self._lock:
            call = self._calls.get(call_id)
            if not call:
                return False
            call.bridge_channel_id = channel_id
            return True

    def bridge_call(self, call_id: str) -> bool:
        """Transition to BRIDGED state.

        Called when both legs are connected and bridged.
        """
        return self.transition(call_id, CallState.BRIDGED)

    def hangup(self, call_id: str, reason: Optional[str] = None) -> bool:
        """Transition to HANGUP. Optionally record the reason."""
        ok = self.transition(call_id, CallState.HANGUP)
        if ok and reason:
            call = self.get(call_id)
            if call:
                call.error = reason
        return ok

    def reject(self, call_id: str, reason: Optional[str] = None) -> bool:
        """Transition to REJECTED. Optionally record the reason."""
        ok = self.transition(call_id, CallState.REJECTED)
        if ok and reason:
            call = self.get(call_id)
            if call:
                call.error = reason
        return ok

    def fallback_to_voicemail(self, call_id: str) -> bool:
        """Transition to VOICEMAIL. Called on ring timeout."""
        return self.transition(call_id, CallState.VOICEMAIL)

    # -- Outgoing-specific orchestration --

    def start_telegram_calling(self, call_id: str) -> bool:
        """Transition from MODEM_RESERVED → TELEGRAM_CALLING.

        Called when the Telegram call invitation is sent.
        """
        return self.transition(call_id, CallState.TELEGRAM_CALLING)

    def user_accepted(self, call_id: str) -> bool:
        """Transition from TELEGRAM_CALLING → USER_ACCEPTED.

        Called when the Telegram user answers the outgoing call.
        """
        return self.transition(call_id, CallState.USER_ACCEPTED)

    def dial_gsm(self, call_id: str, gsm_channel_id: Optional[str] = None) -> bool:
        """Transition from USER_ACCEPTED → GSM_DIALING.

        Called when we start dialing the GSM number.
        """
        ok = self.transition(call_id, CallState.GSM_DIALING)
        if ok and gsm_channel_id:
            call = self.get(call_id)
            if call:
                call.gsm_channel_id = gsm_channel_id
        return ok

    def gsm_ringing(self, call_id: str) -> bool:
        """Transition from GSM_DIALING → GSM_RINGING.

        Called when the GSM side is ringing.
        """
        return self.transition(call_id, CallState.GSM_RINGING)

    def gsm_connected(self, call_id: str) -> bool:
        """Transition from GSM_RINGING → CONNECTED.

        Called when the GSM call is answered.
        """
        return self.transition(call_id, CallState.CONNECTED)

    def gsm_busy(self, call_id: str) -> bool:
        """Transition to GSM_BUSY (terminal)."""
        return self.transition(call_id, CallState.GSM_BUSY)

    def gsm_no_answer(self, call_id: str) -> bool:
        """Transition to GSM_NO_ANSWER (terminal)."""
        return self.transition(call_id, CallState.GSM_NO_ANSWER)

    def gsm_error(self, call_id: str, reason: Optional[str] = None) -> bool:
        """Transition to GSM_ERROR (terminal)."""
        ok = self.transition(call_id, CallState.GSM_ERROR)
        if ok and reason:
            call = self.get(call_id)
            if call:
                call.error = reason
        return ok

    def telegram_timeout(self, call_id: str) -> bool:
        """Transition to TELEGRAM_TIMEOUT (terminal)."""
        return self.transition(call_id, CallState.TELEGRAM_TIMEOUT)

    def set_telegram_call_id(self, call_id: str, tg_call_id: str) -> bool:
        """Record the Telegram call session ID."""
        with self._lock:
            call = self._calls.get(call_id)
            if not call:
                return False
            call.telegram_call_id = tg_call_id
            return True

    # -- Timeout/duration checking --

    def get_timed_out_calls(
        self, ring_wait_seconds: int, max_call_seconds: int
    ) -> List[CallMachine]:
        """Return calls that have exceeded their timeout.

        Checks:
        - Ringing calls that exceeded ring_wait_seconds
        - Bridged calls that exceeded max_call_seconds
        """
        now = datetime.now(timezone.utc)
        timed_out = []

        with self._lock:
            for call in self._calls.values():
                if call.is_terminal:
                    continue
                try:
                    created = datetime.fromisoformat(call.created_at)
                    elapsed = (now - created).total_seconds()
                except (ValueError, TypeError):
                    continue

                if call.state in (CallState.RINGING, CallState.TELEGRAM_RINGING):
                    if elapsed > ring_wait_seconds:
                        timed_out.append(call)
                elif call.state == CallState.BRIDGED:
                    if elapsed > max_call_seconds:
                        timed_out.append(call)

        return timed_out

    # -- Orphan channel detection --

    def get_orphan_channel_ids(self) -> List[str]:
        """Return all channel IDs registered to active calls.

        Used to detect orphan channels in Asterisk after cleanup.
        """
        channels = []
        with self._lock:
            for call in self._calls.values():
                if not call.is_terminal:
                    channels.extend(call.get_active_channel_ids())
        return channels

    # -- Bridge health / link drop detection (S04.4) --

    def get_bridged_calls(self) -> List[CallMachine]:
        """Return all calls in BRIDGED state.

        Used for bridge health monitoring — if the Tailscale link drops,
        these calls need to be terminated and the user notified.
        """
        with self._lock:
            return [
                c for c in self._calls.values()
                if c.state == CallState.BRIDGED
            ]

    def terminate_bridged_calls(self, reason: str = "link_drop") -> List[str]:
        """Terminate all bridged calls due to a link failure.

        Returns list of call_ids that were terminated.
        This is the last resort when the Tailscale link drops mid-call.
        """
        terminated = []
        with self._lock:
            for call in list(self._calls.values()):
                if call.state == CallState.BRIDGED:
                    try:
                        call.transition(CallState.HANGUP)
                    except InvalidTransition:
                        pass
                    call.error = reason
                    terminated.append(call.call_id)
                    logger.warning(
                        "Terminated bridged call %s: %s",
                        call.call_id[:8],
                        reason,
                    )
        return terminated
