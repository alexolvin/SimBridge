"""Stage 05 S05.1/S05.2 tests — modem abstraction, pool, routing.

Covers:
- TS05-1  modem_id provenance on call records (unit level; the SMS/delivery
          provenance is covered in test_sms.py / test_userbot_http.py)
- TS05-2  state derived from the device — unplug/replug via the poller
- TS05-3  routing across two modems (round-robin, first-available)
- TS05-4  all-busy / all-offline: clear error, no hang
- TS05-5  atomic reservation under contention

The live unplug/replug against a physical dongle is MANUAL_VERIFY (Pass C);
here the device is a fake AMI, which is the contract the poller depends on.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from core.call_control import CallRegistry, ModemBusyError
from core.events import EventType
from core.modem import (
    AVAILABLE_STATES,
    ExplicitStrategy,
    FirstAvailableStrategy,
    ModemInfo,
    ModemPool,
    ModemState,
    RoundRobinStrategy,
    SingleModemProvider,
    StickyStrategy,
)
from agent.modem_poll import poll_modem_state, run_modem_poller
from tests.test_agent_sms_report import FakeAudit


# =========================================================================
# Test doubles
# =========================================================================


class FakeMultiProvider:
    """Multi-modem provider double for routing/contention tests.

    Implements the ModemProvider contract; states are settable so a test
    can model READY/OFFLINE per modem.
    """

    def __init__(self, modem_ids, ready: bool = True) -> None:
        self._lock = threading.Lock()
        self._infos = {}
        for mid in modem_ids:
            self._infos[mid] = ModemInfo(
                modem_id=mid,
                device=mid,
                state=ModemState.READY if ready else ModemState.OFFLINE,
                registered=ready,
            )

    def set_state(self, modem_id: str, state: ModemState) -> None:
        with self._lock:
            self._infos[modem_id].state = state

    def get_info(self, modem_id: str):
        return self._infos.get(modem_id)

    def list_modems(self):
        return list(self._infos.values())

    def is_available(self, modem_id: str) -> bool:
        info = self._infos.get(modem_id)
        return bool(info) and info.state in AVAILABLE_STATES

    def update_state(self, modem_id, registered, signal_percent=None,
                     operator=None, error=None) -> bool:
        with self._lock:
            info = self._infos.get(modem_id)
            if not info:
                return False
            info.registered = registered
            info.state = ModemState.READY if registered else ModemState.OFFLINE
            return True

    def set_sms_active(self, modem_id: str, active: bool) -> bool:
        with self._lock:
            info = self._infos.get(modem_id)
            if not info:
                return False
            info.state = ModemState.SMS_BUSY if active else ModemState.READY
            return True

    def set_call_active(self, modem_id: str, active: bool) -> bool:
        with self._lock:
            info = self._infos.get(modem_id)
            if not info:
                return False
            info.state = ModemState.CALL_BUSY if active else ModemState.READY
            return True

    def mark_offline(self, modem_id: str) -> bool:
        with self._lock:
            info = self._infos.get(modem_id)
            if not info:
                return False
            info.registered = False
            info.state = ModemState.OFFLINE
            return True


class FakeAMI:
    """AMI double: returns a fixed status, or raises, per configuration."""

    def __init__(self, status=None, error: Exception = None) -> None:
        self._status = status if status is not None else {}
        self._error = error
        self.calls = 0

    async def get_modem_status(self) -> dict:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._status


REGISTERED_ENTRY = {
    "device": "gsm",
    "registered": True,
    "signal_percent": 60,
    "operator": "MTS",
}


def _run(coro):
    """Run *coro* on a fresh loop, leaving the global loop policy intact.

    ``_run()`` resets the policy's current loop to None on exit,
    which breaks test_s06_observability.py's ``get_event_loop()`` usage.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# =========================================================================
# Provider state (S05.1 — states per GPT §7.2)
# =========================================================================


class TestProviderState:
    def test_initial_state_offline(self):
        p = SingleModemProvider("gsm", "gsm")
        assert p.get_info("gsm").state is ModemState.OFFLINE

    def test_registered_with_signal_is_ready(self):
        p = SingleModemProvider("gsm", "gsm")
        p.update_state("gsm", registered=True, signal_percent=50)
        assert p.get_info("gsm").state is ModemState.READY
        assert p.is_available("gsm") is True

    def test_unregistered_with_stale_signal_is_initializing(self):
        p = SingleModemProvider("gsm", "gsm")
        p.update_state("gsm", registered=True, signal_percent=50)
        p.update_state("gsm", registered=False)  # signal stays at 50
        assert p.get_info("gsm").state is ModemState.INITIALIZING

    def test_mark_offline_clears_stale_signal(self):
        """Unplug: a stale signal must NOT keep the state at INITIALIZING."""
        p = SingleModemProvider("gsm", "gsm")
        p.update_state("gsm", registered=True, signal_percent=50)
        assert p.mark_offline("gsm") is True
        info = p.get_info("gsm")
        assert info.state is ModemState.OFFLINE
        assert info.signal_percent is None
        assert info.operator is None
        assert info.registered is False

    def test_call_busy(self):
        p = SingleModemProvider("gsm", "gsm")
        p.update_state("gsm", registered=True, signal_percent=50)
        p.set_call_active("gsm", True)
        assert p.get_info("gsm").state is ModemState.CALL_BUSY
        p.set_call_active("gsm", False)
        assert p.get_info("gsm").state is ModemState.READY

    def test_sms_busy(self):
        p = SingleModemProvider("gsm", "gsm")
        p.update_state("gsm", registered=True, signal_percent=50)
        p.set_sms_active("gsm", True)
        assert p.get_info("gsm").state is ModemState.SMS_BUSY

    def test_unknown_modem_id(self):
        p = SingleModemProvider("gsm", "gsm")
        assert p.get_info("other") is None
        assert p.is_available("other") is False
        assert p.update_state("other", registered=True) is False
        assert p.mark_offline("other") is False

    def test_sim_number_carried_at_modem_level(self):
        """S05.1: the SIM's number lives on the modem (1:1 with the SIM)."""
        p = SingleModemProvider("gsm", "gsm", sim_number="+79261234555")
        assert p.get_info("gsm").sim_number == "+79261234555"


# =========================================================================
# Poller (TS05-2 — state from the device; unplug/replug)
# =========================================================================


class TestPoller:
    def test_device_present_becomes_ready(self):
        p = SingleModemProvider("gsm", "gsm")
        ami = FakeAMI(status=REGISTERED_ENTRY)
        ok = _run(poll_modem_state(ami, p, "gsm"))
        assert ok is True
        assert p.get_info("gsm").state is ModemState.READY
        assert p.get_info("gsm").operator == "MTS"

    def test_device_absent_becomes_offline(self):
        p = SingleModemProvider("gsm", "gsm")
        p.update_state("gsm", registered=True, signal_percent=50)
        ami = FakeAMI(status={})  # no DongleDeviceEntry — unplugged
        ok = _run(poll_modem_state(ami, p, "gsm"))
        assert ok is False
        assert p.get_info("gsm").state is ModemState.OFFLINE

    def test_replug_cycle(self):
        """unplug -> replug must return to READY."""
        p = SingleModemProvider("gsm", "gsm")
        ami = FakeAMI(status=REGISTERED_ENTRY)
        _run(poll_modem_state(ami, p, "gsm"))
        assert p.get_info("gsm").state is ModemState.READY
        # unplug
        ami._status = {}
        _run(poll_modem_state(ami, p, "gsm"))
        assert p.get_info("gsm").state is ModemState.OFFLINE
        # replug
        ami._status = REGISTERED_ENTRY
        ok = _run(poll_modem_state(ami, p, "gsm"))
        assert ok is True
        assert p.get_info("gsm").state is ModemState.READY

    def test_ami_down_keeps_last_state(self):
        """AMI unreachable: state is kept (stale by at most one interval),
        never fabricated, and the poll does not raise."""
        p = SingleModemProvider("gsm", "gsm")
        ami = FakeAMI(status=REGISTERED_ENTRY)
        _run(poll_modem_state(ami, p, "gsm"))
        assert p.get_info("gsm").state is ModemState.READY
        ami._error = ConnectionError("AMI down")
        ok = _run(poll_modem_state(ami, p, "gsm"))
        assert ok is True  # still reports the last known READY
        assert p.get_info("gsm").state is ModemState.READY

    def test_poller_loop_stops_on_event(self):
        p = SingleModemProvider("gsm", "gsm")
        ami = FakeAMI(status=REGISTERED_ENTRY)

        async def run():
            # Event is created inside the running loop (Py3.9 binds
            # loop primitives to the running loop at construction).
            stop = asyncio.Event()
            task = asyncio.create_task(
                run_modem_poller(ami, p, "gsm", interval=0.01, stop=stop)
            )
            # let it poll at least once, then stop
            await asyncio.sleep(0.03)
            stop.set()
            await task
            return ami.calls

        calls = _run(run())
        assert calls >= 1
        assert p.get_info("gsm").state is ModemState.READY


# =========================================================================
# Routing strategies (TS05-3)
# =========================================================================


class TestRouting:
    def test_round_robin_alternates(self):
        provider = FakeMultiProvider(["modem-a", "modem-b"])
        pool = ModemPool(provider=provider, strategy=RoundRobinStrategy())
        assert pool.strategy_name == "round_robin"
        picked = []
        for _ in range(4):
            info = pool.select_for_call()
            picked.append(info.modem_id)
            pool.release(info.modem_id)
        assert picked == ["modem-a", "modem-b", "modem-a", "modem-b"]

    def test_first_available_is_deterministic(self):
        provider = FakeMultiProvider(["modem-b", "modem-a"])
        pool = ModemPool(provider=provider, strategy=FirstAvailableStrategy())
        assert pool.strategy_name == "first_available"
        for _ in range(3):
            info = pool.select_for_call()
            assert info.modem_id == "modem-a"  # sorted by modem_id
            pool.release(info.modem_id)

    def test_default_strategy_is_first_available(self):
        provider = FakeMultiProvider(["modem-a"])
        pool = ModemPool(provider=provider)
        assert pool.strategy_name == "first_available"

    def test_unimplemented_strategies_fail_loudly(self):
        """GPT §8 lists sticky/explicit; unused in any deployment — they
        raise instead of silently routing with the wrong semantics."""
        for strategy in (StickyStrategy(), ExplicitStrategy()):
            with pytest.raises(NotImplementedError):
                strategy.select([], "+79261234555")

    def test_select_empty_returns_none(self):
        provider = FakeMultiProvider(["modem-a"], ready=False)
        pool = ModemPool(provider=provider)
        assert pool.select_for_call() is None
        assert pool.select_for_sms() is None


# =========================================================================
# Atomic reservation under contention (TS05-5)
# =========================================================================


class TestPoolContention:
    def test_two_modems_exactly_two_winners(self):
        provider = FakeMultiProvider(["modem-a", "modem-b"])
        pool = ModemPool(provider=provider)
        winners = []
        lock = threading.Lock()

        def worker():
            info = pool.select_for_call()
            if info:
                with lock:
                    winners.append(info.modem_id)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(winners) == ["modem-a", "modem-b"]
        assert pool.get_reserved_count() == 2

    def test_one_modem_exactly_one_winner(self):
        provider = FakeMultiProvider(["modem-a"])
        pool = ModemPool(provider=provider)
        winners = []
        lock = threading.Lock()

        def worker():
            info = pool.select_for_call()
            if info:
                with lock:
                    winners.append(info.modem_id)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert winners == ["modem-a"]
        assert pool.get_reserved_count() == 1


# =========================================================================
# All-busy / all-offline (TS05-4) + selection audit (S05.2)
# =========================================================================


class TestAllBusy:
    def test_second_call_is_busy(self):
        provider = SingleModemProvider("gsm", "gsm")
        provider.update_state("gsm", registered=True, signal_percent=50)
        pool = ModemPool(provider=provider)
        reg = CallRegistry(
            sms_store=MagicMock(), audit=FakeAudit(), modem_pool=pool
        )
        reg.create_outgoing(callee_number="+14155552671")
        with pytest.raises(ModemBusyError) as ei:
            reg.create_outgoing(callee_number="+14155552672")
        assert ei.value.reason == "busy"

    def test_offline_pool_reports_offline(self):
        provider = SingleModemProvider("gsm", "gsm")  # OFFLINE by default
        pool = ModemPool(provider=provider)
        reg = CallRegistry(
            sms_store=MagicMock(), audit=FakeAudit(), modem_pool=pool
        )
        with pytest.raises(ModemBusyError) as ei:
            reg.create_outgoing(callee_number="+14155552671")
        assert ei.value.reason == "offline"

    def test_all_offline_helper(self):
        assert ModemPool(
            provider=FakeMultiProvider(["a", "b"], ready=False)
        ).all_offline() is True
        assert ModemPool(
            provider=FakeMultiProvider(["a", "b"], ready=True)
        ).all_offline() is False


class TestSelectionAudit:
    def test_pool_selection_is_audited(self):
        provider = SingleModemProvider("ttyUSB0", "ttyUSB0")
        provider.update_state("ttyUSB0", registered=True, signal_percent=50)
        pool = ModemPool(provider=provider)
        audit = FakeAudit()
        reg = CallRegistry(sms_store=MagicMock(), audit=audit, modem_pool=pool)
        call = reg.create_outgoing(callee_number="+14155552671")
        sel = [kw for ev, kw in audit.calls if ev == EventType.MODEM_SELECTED]
        assert len(sel) == 1
        assert sel[0]["modem_id"] == "ttyUSB0"
        assert sel[0]["correlation_id"] == call.call_id
        assert sel[0]["details"] == {
            "policy": "first_available",
            "direction": "outgoing",
            "destination": "+14155552671",
        }

    def test_round_robin_policy_name_in_audit(self):
        provider = FakeMultiProvider(["a", "b"])
        pool = ModemPool(provider=provider, strategy=RoundRobinStrategy())
        audit = FakeAudit()
        reg = CallRegistry(sms_store=MagicMock(), audit=audit, modem_pool=pool)
        reg.create_outgoing(callee_number="+14155552671")
        sel = [kw for ev, kw in audit.calls if ev == EventType.MODEM_SELECTED]
        assert sel[0]["details"]["policy"] == "round_robin"

    def test_no_pool_is_direct(self):
        """Backward-compat path: no pool -> 'direct' is audited too."""
        audit = FakeAudit()
        reg = CallRegistry(sms_store=MagicMock(), audit=audit)
        reg.create_outgoing(callee_number="+14155552671")
        sel = [kw for ev, kw in audit.calls if ev == EventType.MODEM_SELECTED]
        assert sel[0]["details"]["policy"] == "direct"
        assert sel[0]["modem_id"] == "gsm"


# =========================================================================
# Provenance (TS05-1)
# =========================================================================


class TestProvenance:
    def test_outgoing_call_carries_selected_modem(self):
        provider = SingleModemProvider("ttyUSB0", "ttyUSB0")
        provider.update_state("ttyUSB0", registered=True, signal_percent=50)
        pool = ModemPool(provider=provider)
        reg = CallRegistry(
            sms_store=MagicMock(), audit=FakeAudit(), modem_pool=pool
        )
        call = reg.create_outgoing(callee_number="+14155552671")
        assert call.modem_id == "ttyUSB0"

    def test_incoming_call_carries_given_modem(self):
        reg = CallRegistry(sms_store=MagicMock(), audit=FakeAudit())
        call = reg.create_incoming(
            caller_number="+79261234555", modem_id="ttyUSB0"
        )
        assert call.modem_id == "ttyUSB0"

    def test_incoming_default_modem(self):
        reg = CallRegistry(sms_store=MagicMock(), audit=FakeAudit())
        call = reg.create_incoming(caller_number="+79261234555")
        assert call.modem_id == "gsm"
