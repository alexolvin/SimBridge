"""Modem abstraction layer — providers, pools, routing.

S05.1: ModemProvider interface with per-modem state tracking.
S05.2: ModemPool with pluggable routing strategies.

The existing single `gsm` dongle becomes one provider in a one-member pool.
Code paths are identical — not a special case.

States (per GPT §7.2):
    OFFLINE — device not detected
    INITIALIZING — device detected, not yet registered
    READY — registered, available for use
    BUSY — actively handling an action (SMS or call)
    SMS_BUSY — actively sending/receiving SMS
    CALL_BUSY — actively on a voice call
    ERROR — device detected but in an error state
    DISABLED — administratively disabled

Operational state is derived from real device metrics, not tracked
optimistically in memory (it can drift from reality).
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set

logger = logging.getLogger("simbridge.modem")


class ModemState(str, Enum):
    """Modem operational states (GPT §7.2)."""

    OFFLINE = "offline"
    INITIALIZING = "initializing"
    READY = "ready"
    BUSY = "busy"
    SMS_BUSY = "sms_busy"
    CALL_BUSY = "call_busy"
    ERROR = "error"
    DISABLED = "disabled"


# States that can accept new work
AVAILABLE_STATES = {ModemState.READY}

# States that indicate the modem is online (detectable)
ONLINE_STATES = {
    ModemState.INITIALIZING,
    ModemState.READY,
    ModemState.BUSY,
    ModemState.SMS_BUSY,
    ModemState.CALL_BUSY,
}

# States that indicate a broken device worth a recovery attempt.
# Busy states (CALL_BUSY/SMS_BUSY/BUSY) and INITIALIZING are normal
# operation, NOT broken: "recovering" mid-call (e.g. an AMI
# reconnect) would drop the active call's event stream, and a
# registering modem has not failed yet. is_available() must not be
# used as a health signal — it conflates "cannot take new work"
# with "broken".
BROKEN_STATES = {ModemState.OFFLINE, ModemState.ERROR}


def is_broken(info: Optional[ModemInfo]) -> bool:
    """Whether a modem state warrants a recovery attempt (watchdog)."""
    return info is not None and info.state in BROKEN_STATES


@dataclass
class ModemInfo:
    """Snapshot of a modem's current state."""

    modem_id: str
    device: str  # e.g., "gsm", "ttyUSB0"
    state: ModemState
    registered: bool = False
    signal_percent: Optional[int] = None
    operator: Optional[str] = None
    sim_number: Optional[str] = None
    imei_suffix: Optional[str] = None
    error: Optional[str] = None
    last_seen: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict:
        return {
            "modem_id": self.modem_id,
            "device": self.device,
            "state": self.state.value,
            "registered": self.registered,
            "signal_percent": self.signal_percent,
            "operator": self.operator,
            "sim_number": self.sim_number,
            "imei_suffix": self.imei_suffix,
            "error": self.error,
        }


class ModemProvider(ABC):
    """Abstract interface for modem management.

    Implementations must be backed by real device state — not optimistic
    in-memory tracking that can drift from reality.
    """

    @abstractmethod
    def get_info(self, modem_id: str) -> Optional[ModemInfo]:
        """Get current state of a modem. Returns None if modem_id is unknown."""

    @abstractmethod
    def list_modems(self) -> List[ModemInfo]:
        """Enumerate all known modems."""

    @abstractmethod
    def is_available(self, modem_id: str) -> bool:
        """Check if a modem can accept new work (SMS or call)."""

    @abstractmethod
    def update_state(
        self,
        modem_id: str,
        registered: bool,
        signal_percent: Optional[int] = None,
        operator: Optional[str] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Update device-derived state. Returns True if modem was found."""

    @abstractmethod
    def set_sms_active(self, modem_id: str, active: bool) -> bool:
        """Mark SMS activity. Returns True if modem was found."""

    @abstractmethod
    def set_call_active(self, modem_id: str, active: bool) -> bool:
        """Mark call activity. Returns True if modem was found."""

    @abstractmethod
    def mark_offline(self, modem_id: str) -> bool:
        """Mark the modem as gone (unplugged / no device entry reported).

        Clears all device-reported fields (registration, signal, operator,
        error) so a stale signal value cannot keep the state at
        INITIALIZING. Returns True if the modem was known.
        """


class SingleModemProvider(ModemProvider):
    """Single-modem implementation.

    This is the default for single-SIM deployments. The modem_id is
    configurable (defaults to "gsm"). State is derived from real device
    reports via update_state().

    ``sim_number`` is the SIM's phone number (node-level config,
    ``sim.phone``). It is carried at the modem level — every record
    carries the ``modem_id`` that maps 1:1 to this SIM on the node, so
    per-record duplication would be a duplicate mechanism (Rule 1).
    """

    def __init__(
        self,
        modem_id: str = "gsm",
        device: str = "gsm",
        sim_number: Optional[str] = None,
    ) -> None:
        self._modem_id = modem_id
        self._device = device
        self._lock = threading.Lock()
        self._info = ModemInfo(
            modem_id=modem_id,
            device=device,
            state=ModemState.OFFLINE,
            sim_number=sim_number,
        )
        self._sms_active = False
        self._call_active = False
        # True once the device has produced its first observation
        # (update_state or mark_offline). Before that, the state is
        # the constructor default (OFFLINE), not a device report —
        # consumers like the watchdog must not treat it as a failure
        # (boot grace).
        self._observed = False

    def get_info(self, modem_id: str) -> Optional[ModemInfo]:
        if modem_id != self._modem_id:
            return None
        with self._lock:
            return self._derive_info()

    def list_modems(self) -> List[ModemInfo]:
        info = self.get_info(self._modem_id)
        return [info] if info else []

    def is_available(self, modem_id: str) -> bool:
        if modem_id != self._modem_id:
            return False
        info = self.get_info(modem_id)
        return info.state in AVAILABLE_STATES if info else False

    def has_observed(self, modem_id: str) -> bool:
        """Whether the device has produced at least one observation.

        Before the first poll the state is the constructor default
        (OFFLINE), not a device report — a watchdog must not count
        it as a failure (boot grace).
        """
        if modem_id != self._modem_id:
            return False
        with self._lock:
            return self._observed

    def update_state(
        self,
        modem_id: str,
        registered: bool,
        signal_percent: Optional[int] = None,
        operator: Optional[str] = None,
        error: Optional[str] = None,
    ) -> bool:
        if modem_id != self._modem_id:
            return False
        with self._lock:
            self._observed = True
            self._info.registered = registered
            if signal_percent is not None:
                self._info.signal_percent = signal_percent
            if operator is not None:
                self._info.operator = operator
            if error is not None:
                self._info.error = error
            self._info.last_seen = time.monotonic()
            self._derive_state()
        return True

    def set_sms_active(self, modem_id: str, active: bool) -> bool:
        if modem_id != self._modem_id:
            return False
        with self._lock:
            self._sms_active = active
            self._derive_state()
        return True

    def set_call_active(self, modem_id: str, active: bool) -> bool:
        if modem_id != self._modem_id:
            return False
        with self._lock:
            self._call_active = active
            self._derive_state()
        return True

    def mark_offline(self, modem_id: str) -> bool:
        if modem_id != self._modem_id:
            return False
        with self._lock:
            self._observed = True
            self._info.registered = False
            self._info.signal_percent = None
            self._info.operator = None
            self._info.error = None
            self._info.last_seen = time.monotonic()
            self._derive_state()
        return True

    def _derive_state(self) -> None:
        """Derive operational state from device state + activity flags.

        This is not optimistic — it reflects the last known device state
        combined with current activity.
        """
        if self._info.error:
            self._info.state = ModemState.ERROR
        elif not self._info.registered and self._info.last_seen > 0:
            if self._info.signal_percent is not None:
                self._info.state = ModemState.INITIALIZING
            else:
                self._info.state = ModemState.OFFLINE
        elif self._call_active:
            self._info.state = ModemState.CALL_BUSY
        elif self._sms_active:
            self._info.state = ModemState.SMS_BUSY
        elif self._info.registered:
            self._info.state = ModemState.READY
        else:
            self._info.state = ModemState.INITIALIZING

    def _derive_info(self) -> ModemInfo:
        """Create a snapshot copy of current state."""
        return ModemInfo(
            modem_id=self._info.modem_id,
            device=self._info.device,
            state=self._info.state,
            registered=self._info.registered,
            signal_percent=self._info.signal_percent,
            operator=self._info.operator,
            sim_number=self._info.sim_number,
            imei_suffix=self._info.imei_suffix,
            error=self._info.error,
            last_seen=self._info.last_seen,
        )


# =========================================================================
# Routing strategies (S05.2)
# =========================================================================


class RoutingStrategy(ABC):
    """Pluggable routing strategy for modem selection.

    Subclasses carry a ``name`` used in the audit log when a selection
    is recorded (S05.2: "which policy chose which modem").
    """

    name: str = "strategy"

    @abstractmethod
    def select(
        self,
        available: List[ModemInfo],
        destination: Optional[str] = None,
    ) -> Optional[ModemInfo]:
        """Select a modem from the available list.

        Returns None if no modem is suitable.
        """


class FirstAvailableStrategy(RoutingStrategy):
    """Select the first available modem (by modem_id order)."""

    name = "first_available"

    def select(
        self,
        available: List[ModemInfo],
        destination: Optional[str] = None,
    ) -> Optional[ModemInfo]:
        if not available:
            return None
        sorted_modems = sorted(available, key=lambda m: m.modem_id)
        return sorted_modems[0]


class RoundRobinStrategy(RoutingStrategy):
    """Distribute requests across available modems in round-robin order."""

    name = "round_robin"

    def __init__(self) -> None:
        self._counter = 0
        self._lock = threading.Lock()

    def select(
        self,
        available: List[ModemInfo],
        destination: Optional[str] = None,
    ) -> Optional[ModemInfo]:
        if not available:
            return None
        sorted_modems = sorted(available, key=lambda m: m.modem_id)
        with self._lock:
            idx = self._counter % len(sorted_modems)
            self._counter += 1
        return sorted_modems[idx]


class StickyStrategy(RoutingStrategy):
    """Sticky: keep the same modem for a given contact.

    NOT IMPLEMENTED — declared as an interface because the GPT document
    §8 lists it as a routing policy. No current deployment uses it;
    select() raises so a misconfiguration fails loudly instead of
    silently routing with the wrong semantics.
    """

    name = "sticky"

    def select(
        self,
        available: List[ModemInfo],
        destination: Optional[str] = None,
    ) -> Optional[ModemInfo]:
        raise NotImplementedError(
            "StickyStrategy is not implemented (not used in any deployment)"
        )


class ExplicitStrategy(RoutingStrategy):
    """Explicit: operator names the modem (e.g. ``SIM2 +7926...``).

    NOT IMPLEMENTED — declared as an interface because the GPT document
    §8 lists it as a routing policy. No current deployment uses it;
    select() raises so a misconfiguration fails loudly instead of
    silently routing with the wrong semantics.
    """

    name = "explicit"

    def select(
        self,
        available: List[ModemInfo],
        destination: Optional[str] = None,
    ) -> Optional[ModemInfo]:
        raise NotImplementedError(
            "ExplicitStrategy is not implemented (not used in any deployment)"
        )


# =========================================================================
# Modem Pool (S05.2)
# =========================================================================


class ModemPool:
    """Group of modems with routing and atomic reservation.

    A single-modem deployment has one pool with one member — the code
    path is the same, not a special case.
    """

    def __init__(
        self,
        provider: ModemProvider,
        strategy: Optional[RoutingStrategy] = None,
    ) -> None:
        self._provider = provider
        self._strategy = strategy or FirstAvailableStrategy()
        self._lock = threading.Lock()
        self._reserved: Set[str] = set()

    @property
    def provider(self) -> ModemProvider:
        return self._provider

    @property
    def strategy_name(self) -> str:
        """Name of the routing policy in use (for selection audit)."""
        return getattr(
            self._strategy, "name", type(self._strategy).__name__
        )

    def list_modems(self) -> List[ModemInfo]:
        """Get current state of all modems in the pool."""
        return self._provider.list_modems()

    def all_offline(self) -> bool:
        """True if the pool is empty or every modem is OFFLINE (device level).

        Distinguishes "no modem reachable" from "all modems busy" — the
        operator-facing 503 message differs.
        """
        infos = self._provider.list_modems()
        if not infos:
            return True
        return all(info.state is ModemState.OFFLINE for info in infos)

    def select_for_sms(
        self,
        destination: Optional[str] = None,
    ) -> Optional[ModemInfo]:
        """Select a modem for SMS. Atomic: selects and reserves in one lock."""
        with self._lock:
            available = self._get_available()
            if not available:
                return None
            chosen = self._strategy.select(available, destination)
            if not chosen:
                return None
            self._reserved.add(chosen.modem_id)
        return chosen

    def select_for_call(
        self,
        destination: Optional[str] = None,
    ) -> Optional[ModemInfo]:
        """Select a modem for a voice call. Atomic: selects and reserves."""
        with self._lock:
            available = self._get_available()
            if not available:
                return None
            chosen = self._strategy.select(available, destination)
            if not chosen:
                return None
            self._reserved.add(chosen.modem_id)
            self._provider.set_call_active(chosen.modem_id, True)
        return chosen

    def release(self, modem_id: str) -> None:
        """Release a modem reservation."""
        with self._lock:
            self._reserved.discard(modem_id)
        self._provider.set_call_active(modem_id, False)

    def is_all_busy(self) -> bool:
        """Check if all modems in the pool are busy."""
        with self._lock:
            return len(self._get_available()) == 0

    def get_reserved_count(self) -> int:
        with self._lock:
            return len(self._reserved)

    def _get_available(self) -> List[ModemInfo]:
        """Get available modems (not reserved, in READY state).

        Called while self._lock is held.
        """
        available = []
        for info in self._provider.list_modems():
            if info.modem_id in self._reserved:
                continue
            if info.state in AVAILABLE_STATES:
                available.append(info)
        return available
