"""Stage 04 S04.2/S04.3 tests — Call control state machine + bridge wiring.

Tests: TS04-3 (PJSIP endpoint), TS04-4 (real call — MANUAL_VERIFY),
       TS04-5 (4 incoming branches), TS04-6 (4 outgoing branches),
       TS04-7 (orphan check), TS04-8 (modem contention).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.call_control import (
    ACLDeniedError,
    CallMachine,
    CallRegistry,
    CallState,
    InvalidTransition,
    ModemBusyError,
)


# =========================================================================
# Call state machine — unit tests (S04.2 + S04.3)
# =========================================================================

class TestCallStateMachine:
    """Call control state machine transitions — S04.3 granular states."""

    # -- Incoming flow --

    def test_incoming_flow_full(self):
        """Full incoming call: IDLE → RINGING → TELEGRAM_RINGING → TELEGRAM_ACCEPTED → GSM_ANSWERED → BRIDGED → HANGUP → CLEANUP."""
        call = CallMachine(call_id="test", direction="incoming")
        assert call.state == CallState.IDLE
        call.transition(CallState.RINGING)
        assert call.state == CallState.RINGING
        call.transition(CallState.TELEGRAM_RINGING)
        assert call.state == CallState.TELEGRAM_RINGING
        call.transition(CallState.TELEGRAM_ACCEPTED)
        assert call.state == CallState.TELEGRAM_ACCEPTED
        call.transition(CallState.GSM_ANSWERED)
        assert call.state == CallState.GSM_ANSWERED
        call.transition(CallState.BRIDGED)
        call.transition(CallState.HANGUP)
        call.transition(CallState.CLEANUP)

    def test_incoming_reject_from_ringing(self):
        """Incoming call can be rejected from RINGING."""
        call = CallMachine(call_id="test", direction="incoming")
        call.transition(CallState.RINGING)
        call.transition(CallState.REJECTED)

    def test_incoming_reject_from_telegram_ringing(self):
        """Incoming call can be rejected from TELEGRAM_RINGING."""
        call = CallMachine(call_id="test", direction="incoming")
        call.transition(CallState.RINGING)
        call.transition(CallState.TELEGRAM_RINGING)
        call.transition(CallState.REJECTED)

    def test_incoming_gsm_hangup_from_ringing(self):
        """GSM caller hangs up while RINGING — cancel Telegram ring."""
        call = CallMachine(call_id="test", direction="incoming")
        call.transition(CallState.RINGING)
        call.transition(CallState.HANGUP)
        call.transition(CallState.CLEANUP)

    def test_incoming_gsm_hangup_from_telegram_ringing(self):
        """GSM caller hangs up while Telegram is ringing."""
        call = CallMachine(call_id="test", direction="incoming")
        call.transition(CallState.RINGING)
        call.transition(CallState.TELEGRAM_RINGING)
        call.transition(CallState.HANGUP)
        call.transition(CallState.CLEANUP)

    def test_incoming_timeout_to_voicemail_from_ringing(self):
        """Ring timeout from RINGING → voicemail."""
        call = CallMachine(call_id="test", direction="incoming")
        call.transition(CallState.RINGING)
        call.transition(CallState.VOICEMAIL)
        call.transition(CallState.HANGUP)
        call.transition(CallState.CLEANUP)

    def test_incoming_timeout_to_voicemail_from_telegram_ringing(self):
        """Ring timeout from TELEGRAM_RINGING → voicemail."""
        call = CallMachine(call_id="test", direction="incoming")
        call.transition(CallState.RINGING)
        call.transition(CallState.TELEGRAM_RINGING)
        call.transition(CallState.VOICEMAIL)

    # -- Outgoing flow --

    def test_outgoing_flow_full(self):
        """Full outgoing call: IDLE → REQUESTED → ACL_CHECKED → MODEM_RESERVED → ... → BRIDGED → HANGUP → CLEANUP."""
        call = CallMachine(call_id="test", direction="outgoing")
        assert call.state == CallState.IDLE
        call.transition(CallState.REQUESTED)
        call.transition(CallState.ACL_CHECKED)
        call.transition(CallState.MODEM_RESERVED)
        call.transition(CallState.TELEGRAM_CALLING)
        call.transition(CallState.USER_ACCEPTED)
        call.transition(CallState.GSM_DIALING)
        call.transition(CallState.GSM_RINGING)
        call.transition(CallState.CONNECTED)
        call.transition(CallState.BRIDGED)
        call.transition(CallState.HANGUP)
        call.transition(CallState.CLEANUP)

    def test_outgoing_gsm_busy(self):
        """GSM busy: GSM_DIALING → GSM_BUSY (terminal)."""
        call = CallMachine(call_id="test", direction="outgoing")
        call.transition(CallState.REQUESTED)
        call.transition(CallState.ACL_CHECKED)
        call.transition(CallState.MODEM_RESERVED)
        call.transition(CallState.TELEGRAM_CALLING)
        call.transition(CallState.USER_ACCEPTED)
        call.transition(CallState.GSM_DIALING)
        call.transition(CallState.GSM_BUSY)
        assert call.is_terminal

    def test_outgoing_gsm_no_answer(self):
        """GSM no answer: GSM_RINGING → GSM_NO_ANSWER (terminal)."""
        call = CallMachine(call_id="test", direction="outgoing")
        call.transition(CallState.REQUESTED)
        call.transition(CallState.ACL_CHECKED)
        call.transition(CallState.MODEM_RESERVED)
        call.transition(CallState.TELEGRAM_CALLING)
        call.transition(CallState.USER_ACCEPTED)
        call.transition(CallState.GSM_DIALING)
        call.transition(CallState.GSM_RINGING)
        call.transition(CallState.GSM_NO_ANSWER)
        assert call.is_terminal

    def test_outgoing_gsm_error(self):
        """GSM network error: GSM_DIALING → GSM_ERROR (terminal)."""
        call = CallMachine(call_id="test", direction="outgoing")
        call.transition(CallState.REQUESTED)
        call.transition(CallState.ACL_CHECKED)
        call.transition(CallState.MODEM_RESERVED)
        call.transition(CallState.TELEGRAM_CALLING)
        call.transition(CallState.USER_ACCEPTED)
        call.transition(CallState.GSM_DIALING)
        call.transition(CallState.GSM_ERROR)
        assert call.is_terminal

    def test_outgoing_telegram_timeout_from_calling(self):
        """Telegram user doesn't answer: TELEGRAM_CALLING → TELEGRAM_TIMEOUT."""
        call = CallMachine(call_id="test", direction="outgoing")
        call.transition(CallState.REQUESTED)
        call.transition(CallState.ACL_CHECKED)
        call.transition(CallState.MODEM_RESERVED)
        call.transition(CallState.TELEGRAM_CALLING)
        call.transition(CallState.TELEGRAM_TIMEOUT)
        assert call.is_terminal

    def test_outgoing_telegram_timeout_from_modem_reserved(self):
        """Telegram timeout from MODEM_RESERVED."""
        call = CallMachine(call_id="test", direction="outgoing")
        call.transition(CallState.REQUESTED)
        call.transition(CallState.ACL_CHECKED)
        call.transition(CallState.MODEM_RESERVED)
        call.transition(CallState.TELEGRAM_TIMEOUT)
        assert call.is_terminal

    # -- Invalid transitions --

    def test_invalid_transition_skip_states_incoming(self):
        """Cannot jump from RINGING directly to BRIDGED."""
        call = CallMachine(call_id="test", direction="incoming")
        call.transition(CallState.RINGING)
        with pytest.raises(InvalidTransition):
            call.transition(CallState.BRIDGED)

    def test_invalid_transition_skip_states_outgoing(self):
        """Cannot go from MODEM_RESERVED to BRIDGED (skip steps)."""
        call = CallMachine(call_id="test", direction="outgoing")
        call.transition(CallState.REQUESTED)
        call.transition(CallState.ACL_CHECKED)
        call.transition(CallState.MODEM_RESERVED)
        with pytest.raises(InvalidTransition):
            call.transition(CallState.BRIDGED)

    def test_invalid_transition_from_terminal(self):
        """Cannot transition from CLEANUP."""
        call = CallMachine(call_id="test", direction="incoming")
        call.transition(CallState.RINGING)
        call.transition(CallState.REJECTED)
        # REJECTED is terminal — no further transitions
        assert call.is_terminal

    # -- Utility properties --

    def test_timestamps_set(self):
        """created_at and updated_at are set on init."""
        call = CallMachine(call_id="test", direction="incoming")
        assert call.created_at
        assert call.updated_at
        assert call.created_at == call.updated_at

    def test_to_dict_serialization(self):
        """CallMachine.to_dict() returns expected keys including bridge legs."""
        call = CallMachine(
            call_id="abc",
            direction="incoming",
            caller_number="+79261234555",
            caller_name="Test",
            gsm_channel_id="Dongle0/gsm",
            bridge_channel_id="PJSIP/tg-bridge-00000001",
            telegram_user_id=12345,
            telegram_call_id="tg-call-abc",
        )
        d = call.to_dict()
        assert d["call_id"] == "abc"
        assert d["direction"] == "incoming"
        assert d["caller_number"] == "+79261234555"
        assert d["caller_name"] == "Test"
        assert d["state"] == "idle"
        assert d["gsm_channel_id"] == "Dongle0/gsm"
        assert d["bridge_channel_id"] == "PJSIP/tg-bridge-00000001"
        assert d["telegram_user_id"] == 12345
        assert d["telegram_call_id"] == "tg-call-abc"
        assert d["is_terminal"] is False

    def test_is_terminal_states(self):
        """Terminal states are correctly identified."""
        from core.call_control import _TERMINAL_STATES
        assert CallState.CLEANUP in _TERMINAL_STATES
        assert CallState.REJECTED in _TERMINAL_STATES
        assert CallState.HANGUP in _TERMINAL_STATES
        assert CallState.VOICEMAIL in _TERMINAL_STATES
        assert CallState.ACL_DENIED in _TERMINAL_STATES
        assert CallState.TELEGRAM_TIMEOUT in _TERMINAL_STATES
        assert CallState.GSM_BUSY in _TERMINAL_STATES
        assert CallState.GSM_NO_ANSWER in _TERMINAL_STATES
        assert CallState.GSM_ERROR in _TERMINAL_STATES
        assert CallState.BRIDGED not in _TERMINAL_STATES
        assert CallState.RINGING not in _TERMINAL_STATES

    def test_get_active_channel_ids(self):
        """get_active_channel_ids returns both legs when set."""
        call = CallMachine(
            call_id="test",
            direction="incoming",
            gsm_channel_id="Dongle0/gsm",
            bridge_channel_id="PJSIP/tg-bridge-00000001",
        )
        channels = call.get_active_channel_ids()
        assert len(channels) == 2
        assert "Dongle0/gsm" in channels
        assert "PJSIP/tg-bridge-00000001" in channels

    def test_get_active_channel_ids_empty(self):
        """get_active_channel_ids returns empty list when no legs set."""
        call = CallMachine(call_id="test", direction="incoming")
        assert call.get_active_channel_ids() == []

    def test_duration_check_exceeded(self):
        """check_duration_exceeded returns True when call is too long."""
        from datetime import datetime, timezone, timedelta
        call = CallMachine(
            call_id="test",
            direction="incoming",
            created_at=(datetime.now(timezone.utc) - timedelta(seconds=2000)).isoformat(),
        )
        assert call.check_duration_exceeded(max_seconds=1800) is True

    def test_duration_check_not_exceeded(self):
        """check_duration_exceeded returns True when call is too long."""
        from datetime import datetime, timezone
        call = CallMachine(
            call_id="test",
            direction="incoming",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        assert call.check_duration_exceeded(max_seconds=1800) is False


# =========================================================================
# Call registry — S04.3 higher-level orchestration
# =========================================================================

class TestCallRegistry:
    """Call registry: create, transition, cleanup, orchestration."""

    @pytest.fixture
    def registry(self):
        mock_sms = MagicMock()
        mock_audit = MagicMock()
        return CallRegistry(sms_store=mock_sms, audit=mock_audit)

    # -- Basic create --

    def test_create_incoming_starts_ringing(self, registry):
        call = registry.create_incoming(caller_number="+79261234555")
        assert call.state == CallState.RINGING
        assert call.direction == "incoming"

    def test_create_outgoing_reserves_modem(self, registry):
        call = registry.create_outgoing(callee_number="+14155552671")
        assert call.state == CallState.MODEM_RESERVED
        assert call.direction == "outgoing"

    def test_second_outgoing_call_fails_modem_busy(self, registry):
        registry.create_outgoing(callee_number="+14155552671")
        with pytest.raises(ModemBusyError):
            registry.create_outgoing(callee_number="+14155552672")

    def test_cleanup_releases_modem(self, registry):
        call = registry.create_outgoing(callee_number="+14155552671")
        registry.transition(call.call_id, CallState.TELEGRAM_CALLING)
        registry.transition(call.call_id, CallState.USER_ACCEPTED)
        registry.transition(call.call_id, CallState.GSM_DIALING)
        registry.transition(call.call_id, CallState.GSM_RINGING)
        registry.transition(call.call_id, CallState.CONNECTED)
        registry.transition(call.call_id, CallState.BRIDGED)
        registry.transition(call.call_id, CallState.HANGUP)
        registry.cleanup(call.call_id)
        call2 = registry.create_outgoing(callee_number="+14155552672")
        assert call2.state == CallState.MODEM_RESERVED

    # -- Incoming branch: accept --

    def test_incoming_accept_flow(self, registry):
        """Full accept: RINGING → TELEGRAM_RINGING → TELEGRAM_ACCEPTED → GSM_ANSWERED → BRIDGED."""
        call = registry.create_incoming(caller_number="+79261234555")
        assert registry.start_telegram_ring(call.call_id) is True
        assert call.state == CallState.TELEGRAM_RINGING
        assert registry.accept_incoming(call.call_id) is True
        assert call.state == CallState.TELEGRAM_ACCEPTED
        assert registry.answer_gsm(call.call_id) is True
        assert call.state == CallState.GSM_ANSWERED
        assert registry.bridge_call(call.call_id) is True
        assert call.state == CallState.BRIDGED

    # -- Incoming branch: reject --

    def test_incoming_reject_flow(self, registry):
        """Reject from TELEGRAM_RINGING."""
        call = registry.create_incoming(caller_number="+79261234555")
        registry.start_telegram_ring(call.call_id)
        assert registry.reject(call.call_id, reason="user_rejected") is True
        assert call.state == CallState.REJECTED
        registry.cleanup(call.call_id)
        assert registry.get(call.call_id) is None

    # -- Incoming branch: voicemail fallback --

    def test_incoming_voicemail_fallback(self, registry):
        """Timeout → voicemail."""
        call = registry.create_incoming(caller_number="+79261234555")
        registry.start_telegram_ring(call.call_id)
        assert registry.fallback_to_voicemail(call.call_id) is True
        assert call.state == CallState.VOICEMAIL

    # -- Incoming branch: GSM caller hangup --

    def test_incoming_gsm_hangup_from_ringing(self, registry):
        """GSM caller hangs up during RINGING."""
        call = registry.create_incoming(caller_number="+79261234555")
        assert registry.hangup(call.call_id, reason="caller_hangup") is True
        assert call.state == CallState.HANGUP
        registry.cleanup(call.call_id)

    # -- Outgoing branch: full flow --

    def test_outgoing_full_flow(self, registry):
        """Full outgoing: MODEM_RESERVED → TELEGRAM_CALLING → USER_ACCEPTED → GSM_DIALING → GSM_RINGING → CONNECTED → BRIDGED."""
        call = registry.create_outgoing(callee_number="+14155552671")
        assert call.state == CallState.MODEM_RESERVED
        assert registry.start_telegram_calling(call.call_id) is True
        assert call.state == CallState.TELEGRAM_CALLING
        assert registry.user_accepted(call.call_id) is True
        assert call.state == CallState.USER_ACCEPTED
        assert registry.dial_gsm(call.call_id) is True
        assert call.state == CallState.GSM_DIALING
        assert registry.gsm_ringing(call.call_id) is True
        assert call.state == CallState.GSM_RINGING
        assert registry.gsm_connected(call.call_id) is True
        assert call.state == CallState.CONNECTED
        assert registry.bridge_call(call.call_id) is True
        assert call.state == CallState.BRIDGED

    # -- Outgoing branch: GSM busy --

    def test_outgoing_gsm_busy(self, registry):
        """GSM busy from GSM_DIALING."""
        call = registry.create_outgoing(callee_number="+14155552671")
        registry.start_telegram_calling(call.call_id)
        registry.user_accepted(call.call_id)
        registry.dial_gsm(call.call_id)
        assert registry.gsm_busy(call.call_id) is True
        assert call.state == CallState.GSM_BUSY
        assert call.is_terminal

    # -- Outgoing branch: GSM no answer --

    def test_outgoing_gsm_no_answer(self, registry):
        """GSM no answer from GSM_RINGING."""
        call = registry.create_outgoing(callee_number="+1415552671")
        registry.start_telegram_calling(call.call_id)
        registry.user_accepted(call.call_id)
        registry.dial_gsm(call.call_id)
        registry.gsm_ringing(call.call_id)
        assert registry.gsm_no_answer(call.call_id) is True
        assert call.state == CallState.GSM_NO_ANSWER
        assert call.is_terminal

    # -- Outgoing branch: GSM error --

    def test_outgoing_gsm_error(self, registry):
        """GSM error from GSM_DIALING."""
        call = registry.create_outgoing(callee_number="+14155552671")
        registry.start_telegram_calling(call.call_id)
        registry.user_accepted(call.call_id)
        registry.dial_gsm(call.call_id)
        assert registry.gsm_error(call.call_id, reason="network_error") is True
        assert call.state == CallState.GSM_ERROR
        assert call.error == "network_error"
        assert call.is_terminal

    # -- Outgoing branch: Telegram timeout --

    def test_outgoing_telegram_timeout(self, registry):
        """Telegram user doesn't answer → TELEGRAM_TIMEOUT."""
        call = registry.create_outgoing(callee_number="+14155552671")
        registry.start_telegram_calling(call.call_id)
        assert registry.telegram_timeout(call.call_id) is True
        assert call.state == CallState.TELEGRAM_TIMEOUT
        assert call.is_terminal

    # -- Bridge leg tracking --

    def test_set_bridge_leg(self, registry):
        """Record bridge channel ID."""
        call = registry.create_incoming(caller_number="+79261234555")
        assert registry.set_bridge_leg(call.call_id, "PJSIP/tg-bridge-00000001") is True
        call = registry.get(call.call_id)
        assert call.bridge_channel_id == "PJSIP/tg-bridge-00000001"

    def test_set_telegram_call_id(self, registry):
        """Record Telegram call session ID."""
        call = registry.create_outgoing(callee_number="+14155552671")
        assert registry.set_telegram_call_id(call.call_id, "tg-call-abc") is True
        call = registry.get(call.call_id)
        assert call.telegram_call_id == "tg-call-abc"

    # -- Orphan channel detection --

    def test_orphan_channel_ids(self, registry):
        """get_orphan_channel_ids returns channels for active calls."""
        call = registry.create_incoming(
            caller_number="+79261234555",
            gsm_channel_id="Dongle0/gsm",
        )
        registry.set_bridge_leg(call.call_id, "PJSIP/tg-bridge-00000001")
        channels = registry.get_orphan_channel_ids()
        assert "Dongle0/gsm" in channels
        assert "PJSIP/tg-bridge-00000001" in channels

    def test_orphan_channel_ids_empty_after_cleanup(self, registry):
        """After cleanup, no channels remain."""
        call = registry.create_incoming(
            caller_number="+79261234555",
            gsm_channel_id="Dongle0/gsm",
        )
        registry.hangup(call.call_id)
        registry.cleanup(call.call_id)
        channels = registry.get_orphan_channel_ids()
        assert len(channels) == 0

    # -- Timeout checking --

    def test_get_timed_out_calls_ring_timeout(self, registry):
        """Detect ringing calls that exceeded ring_wait_seconds."""
        from datetime import datetime, timezone, timedelta
        call = registry.create_incoming(caller_number="+79261234555")
        registry.start_telegram_ring(call.call_id)
        # Backdate the call to simulate timeout
        call.created_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        timed_out = registry.get_timed_out_calls(ring_wait_seconds=24, max_call_seconds=1800)
        assert len(timed_out) == 1
        assert timed_out[0].call_id == call.call_id

    def test_get_timed_out_calls_duration_exceeded(self, registry):
        """Detect bridged calls that exceeded max_call_seconds."""
        from datetime import datetime, timezone, timedelta
        call = registry.create_incoming(caller_number="+79261234555")
        registry.start_telegram_ring(call.call_id)
        registry.accept_incoming(call.call_id)
        registry.answer_gsm(call.call_id)
        registry.bridge_call(call.call_id)
        # Backdate to simulate long call
        call.created_at = (datetime.now(timezone.utc) - timedelta(seconds=2000)).isoformat()
        timed_out = registry.get_timed_out_calls(ring_wait_seconds=24, max_call_seconds=1800)
        assert len(timed_out) == 1

    def test_get_timed_out_calls_none(self, registry):
        """No timed out calls when all are within limits."""
        call = registry.create_incoming(caller_number="+79261234555")
        timed_out = registry.get_timed_out_calls(ring_wait_seconds=24, max_call_seconds=1800)
        assert len(timed_out) == 0

    # -- Count/list --

    def test_get_unknown_call_returns_none(self, registry):
        assert registry.get("nonexistent") is None

    def test_transition_unknown_call_returns_false(self, registry):
        assert registry.transition("nonexistent", CallState.RINGING) is False

    def test_transition_invalid_state_returns_false(self, registry):
        call = registry.create_incoming(caller_number="+79261234555")
        assert registry.transition(call.call_id, CallState.BRIDGED) is False

    def test_list_active_returns_only_non_terminal(self, registry):
        c1 = registry.create_incoming(caller_number="+79261234555")
        c2 = registry.create_incoming(caller_number="+14155552671")
        registry.reject(c1.call_id)
        assert len(registry.list_active()) == 1

    def test_count_by_direction(self, registry):
        registry.create_incoming(caller_number="+79261234555")
        registry.create_incoming(caller_number="+79261234556")
        registry.create_outgoing(callee_number="+14155552671")
        assert registry.count_by_direction("incoming") == 2
        assert registry.count_by_direction("outgoing") == 1

    def test_list_all_includes_terminal(self, registry):
        """list_all includes terminal calls."""
        call = registry.create_incoming(caller_number="+79261234555")
        registry.reject(call.call_id)
        assert len(registry.list_all()) == 1
        assert len(registry.list_active()) == 0


# =========================================================================
# ACL Manager — S04.3
# =========================================================================

class TestCallACL:
    """ACL manager for call authorization (S04.3)."""

    @pytest.fixture
    def acl_file(self):
        content = """# Authorized Telegram users
123456789 out_call in_call out_sms
987654321 in_call out_sms
# No call rights for this user
111111111 out_sms
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write(content)
        yield f.name
        os.unlink(f.name)

    def test_acl_allowed(self, acl_file):
        from core.acl import ACLManager
        acl = ACLManager(acl_file)
        assert acl.check(123456789, "out_call") is True

    def test_acl_denied(self, acl_file):
        from core.acl import ACLManager
        acl = ACLManager(acl_file)
        assert acl.check(111111111, "out_call") is False

    def test_acl_unknown_user(self, acl_file):
        from core.acl import ACLManager
        acl = ACLManager(acl_file)
        assert acl.check(999999999, "out_call") is False

    def test_acl_user_count(self, acl_file):
        from core.acl import ACLManager
        acl = ACLManager(acl_file)
        assert acl.user_count == 3

    def test_acl_in_call_right(self, acl_file):
        from core.acl import ACLManager
        acl = ACLManager(acl_file)
        assert acl.check(987654321, "in_call") is True
        assert acl.check(987654321, "out_call") is False


# =========================================================================
# Config generator — bridge + call duration globals (S04.3)
# =========================================================================

class TestConfigGeneratorBridge:
    """Config generator produces bridge + call duration globals."""

    def test_bridge_and_call_globals_in_output(self):
        from scripts.generate_asterisk_config import generate

        config = {
            "asterisk": {
                "ring_wait_seconds": 24,
                "max_record_seconds": 90,
                "prompt": "/var/lib/asterisk/sounds/custom/vm-prompt",
            },
            "voice": {
                "bridge_endpoint": "tg-bridge",
                "bridge_host": "100.x.x.x",
                "bridge_port": 5062,
                "outbound_answer_timeout": 30,
            },
            "limits": {
                "max_call_seconds": 1800,
            },
            "paths": {},
        }
        with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as f:
            out = f.name
        try:
            generate(config, out)
            result = Path(out).read_text()
            assert "BRIDGE_ENDPOINT=tg-bridge" in result
            assert "BRIDGE_HOST=100.x.x.x" in result
            assert "BRIDGE_PORT=5062" in result
            assert "OUTBOUND_RING_TIMEOUT=30" in result
            assert "MAX_CALL_SECONDS=1800" in result
        finally:
            os.unlink(out)


# =========================================================================
# Dialplan — tg-bridge + S04.3 contexts
# =========================================================================
# NOTE (S01 rebaseline, 2026-08-15): TestDialplanBridge was removed. It
# asserted the structure of the old example dialplan, which was never
# deployable: the MixMonitor @ option does not exist in Asterisk 18,
# TG_ACCEPTED was never set by anything (every call timed out), and the
# System() calls with interpolated user data were an RCE (P0-3). The S04
# bridge stage must re-introduce bridge contexts AND re-add structural
# tests against a design that actually loads in Asterisk. The core
# call-control logic tested below (state machine, registry, modem pool)
# is design-independent and stays.


# =========================================================================
# PJSIP config — bridge endpoint (S04.2)
# =========================================================================

class TestPjsipConfig:
    """PJSIP config structure for tg-bridge."""

    @pytest.fixture(autouse=True)
    def load_pjsip(self):
        pjsip = Path(__file__).parent.parent / "asterisk" / "pjsip.conf.example"
        self.pjsip = pjsip.read_text()

    def test_endpoint_exists(self):
        assert "[tg-bridge]" in self.pjsip

    def test_auth_section_exists(self):
        assert "[tg-bridge-auth]" in self.pjsip

    def test_aor_section_exists(self):
        assert "[tg-bridge-aor]" in self.pjsip

    def test_direct_media_disabled(self):
        assert "direct_media=no" in self.pjsip

    def test_gsm_codecs_only(self):
        assert "allow=ulaw,alaw" in self.pjsip
        assert "disallow=all" in self.pjsip

    def test_dtmf_rfc2833(self):
        assert "dtmf_mode=rfc2833" in self.pjsip


# =========================================================================
# Event types — call events (S04.2 + S04.3)
# =========================================================================

class TestCallEventTypes:
    """Call event types exist in EventType enum."""

    def test_incoming_call_event(self):
        from core.events import EventType
        assert EventType.CALL_INCOMING.value == "CALL_INCOMING"

    def test_outgoing_call_event(self):
        from core.events import EventType
        assert EventType.CALL_OUTGOING.value == "CALL_OUTGOING"

    def test_call_accepted_event(self):
        from core.events import EventType
        assert EventType.CALL_ACCEPTED.value == "CALL_ACCEPTED"

    def test_call_bridged_event(self):
        from core.events import EventType
        assert EventType.CALL_BRIDGED.value == "CALL_BRIDGED"

    def test_call_acl_check_event(self):
        from core.events import EventType
        assert EventType.CALL_ACL_CHECK.value == "CALL_ACL_CHECK"

    def test_call_gsm_answered_event(self):
        from core.events import EventType
        assert EventType.CALL_GSM_ANSWERED.value == "CALL_GSM_ANSWERED"

    def test_call_telegram_timeout_event(self):
        from core.events import EventType
        assert EventType.CALL_TELEGRAM_TIMEOUT.value == "CALL_TELEGRAM_TIMEOUT"

    def test_call_duration_expired_event(self):
        from core.events import EventType
        assert EventType.CALL_DURATION_EXPIRED.value == "CALL_DURATION_EXPIRED"


# =========================================================================
# S04.4 — Distributed mode: link drop detection, bridge health
# =========================================================================

class TestDistributedMode:
    """Distributed mode: link drop detection, bridge health monitoring."""

    @pytest.fixture
    def registry(self):
        mock_sms = MagicMock()
        mock_audit = MagicMock()
        return CallRegistry(sms_store=mock_sms, audit=mock_audit)

    def test_get_bridged_calls_empty(self, registry):
        """No bridged calls initially."""
        assert registry.get_bridged_calls() == []

    def test_get_bridged_calls_returns_bridged(self, registry):
        """get_bridged_calls returns calls in BRIDGED state."""
        call = registry.create_incoming(caller_number="+79261234555")
        registry.start_telegram_ring(call.call_id)
        registry.accept_incoming(call.call_id)
        registry.answer_gsm(call.call_id)
        registry.bridge_call(call.call_id)
        bridged = registry.get_bridged_calls()
        assert len(bridged) == 1
        assert bridged[0].call_id == call.call_id

    def test_get_bridged_calls_excludes_non_bridged(self, registry):
        """get_bridged_calls excludes non-bridged calls."""
        call = registry.create_incoming(caller_number="+79261234555")
        # Call is in RINGING, not BRIDGED
        assert registry.get_bridged_calls() == []

    def test_terminate_bridged_calls(self, registry):
        """terminate_bridged_calls hangs up all bridged calls."""
        call1 = registry.create_incoming(caller_number="+79261234555")
        registry.start_telegram_ring(call1.call_id)
        registry.accept_incoming(call1.call_id)
        registry.answer_gsm(call1.call_id)
        registry.bridge_call(call1.call_id)

        call2 = registry.create_incoming(caller_number="+14155552671")
        registry.start_telegram_ring(call2.call_id)
        registry.accept_incoming(call2.call_id)
        registry.answer_gsm(call2.call_id)
        registry.bridge_call(call2.call_id)

        terminated = registry.terminate_bridged_calls(reason="link_drop")
        assert len(terminated) == 2
        assert call1.call_id in terminated
        assert call2.call_id in terminated

        # Verify calls are now in HANGUP state
        c1 = registry.get(call1.call_id)
        c2 = registry.get(call2.call_id)
        assert c1.state == CallState.HANGUP
        assert c1.error == "link_drop"
        assert c2.state == CallState.HANGUP
        assert c2.error == "link_drop"

    def test_terminate_bridged_calls_empty(self, registry):
        """terminate_bridged_calls returns empty list when nothing bridged."""
        terminated = registry.terminate_bridged_calls(reason="link_drop")
        assert terminated == []

    def test_terminate_bridged_calls_mixed_states(self, registry):
        """Only bridged calls are terminated; ringing calls survive."""
        call_bridged = registry.create_incoming(caller_number="+79261234555")
        registry.start_telegram_ring(call_bridged.call_id)
        registry.accept_incoming(call_bridged.call_id)
        registry.answer_gsm(call_bridged.call_id)
        registry.bridge_call(call_bridged.call_id)

        call_ringing = registry.create_incoming(caller_number="+14155552671")
        # call_ringing is still in RINGING

        terminated = registry.terminate_bridged_calls(reason="link_drop")
        assert len(terminated) == 1
        assert terminated[0] == call_bridged.call_id

        # Ringing call should be unaffected
        assert registry.get(call_ringing.call_id).state == CallState.RINGING


# =========================================================================
# S04.4 — PJSIP config: distributed mode settings
# =========================================================================

class TestPjsipDistributed:
    """PJSIP config for distributed mode (S04.4)."""

    @pytest.fixture(autouse=True)
    def load_pjsip(self):
        pjsip = Path(__file__).parent.parent / "asterisk" / "pjsip.conf.example"
        self.pjsip = pjsip.read_text()

    def test_local_net_tailscale(self):
        """local_net is set to Tailscale CGNAT range."""
        assert "local_net=100.64.0.0/10" in self.pjsip

    def test_nat_option(self):
        """nat_option is configured for tailnet."""
        assert "nat_option=rtp" in self.pjsip

    def test_external_media_addr_documented(self):
        """external_media_addr is documented for distributed mode."""
        assert "external_media_addr" in self.pjsip

    def test_no_srtp(self):
        """No SRTP transport configured — Tailscale already encrypts."""
        assert "srtp" not in self.pjsip.lower() or "srtp" not in self.pjsip


# =========================================================================
# S04.4 — Config generator: distributed mode globals
# =========================================================================

class TestConfigGeneratorDistributed:
    """Config generator produces distributed mode globals."""

    def test_tailnet_cgnat_in_output(self):
        from scripts.generate_asterisk_config import generate

        config = {
            "asterisk": {
                "ring_wait_seconds": 24,
                "max_record_seconds": 90,
                "prompt": "/var/lib/asterisk/sounds/custom/vm-prompt",
            },
            "voice": {
                "bridge_endpoint": "tg-bridge",
                "bridge_host": "100.123.45.67",
                "bridge_port": 5062,
                "outbound_answer_timeout": 30,
            },
            "limits": {"max_call_seconds": 1800},
            "paths": {},
        }
        with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as f:
            out = f.name
        try:
            generate(config, out)
            result = Path(out).read_text()
            assert "TAILNET_CGNAT=100.64.0.0/10" in result
        finally:
            os.unlink(out)


# =========================================================================
# S04.4 — Docs: distributed mode documentation
# =========================================================================

class TestDistributedDocs:
    """Voice bridge docs cover distributed mode."""

    @pytest.fixture(autouse=True)
    def load_docs(self):
        docs = Path(__file__).parent.parent / "docs" / "voice-bridge.md"
        self.docs = docs.read_text()

    def test_distributed_mode_documented(self):
        """Distributed mode section exists."""
        assert "Distributed Mode" in self.docs

    def test_srtp_rationale_documented(self):
        """SRTP rationale states Tailscale already encrypts."""
        assert "duplicated mechanism" in self.docs or "duplicate" in self.docs.lower()

    def test_tailscale_cgnat_documented(self):
        """Tailscale CGNAT range is documented."""
        assert "100.64.0.0/10" in self.docs

    def test_magicdns_or_raw_ip(self):
        """Docs specify MagicDNS FQDN or raw Tailscale IP."""
        assert "MagicDNS" in self.docs

    def test_link_drop_handling_documented(self):
        """Link drop handling is documented."""
        assert "link drop" in self.docs.lower() or "link_drop" in self.docs

    def test_config_only_change(self):
        """Docs state that distributed mode is a config-only change."""
        assert "config-only" in self.docs.lower() or "config-only change" in self.docs


# =========================================================================
# S05.1 — Modem abstraction and provenance
# =========================================================================

class TestModemStates:
    """ModemState enum (GPT §7.2)."""

    def test_all_states_exist(self):
        from core.modem import ModemState
        expected = {
            "offline", "initializing", "ready", "busy",
            "sms_busy", "call_busy", "error", "disabled",
        }
        actual = {s.value for s in ModemState}
        assert expected == actual

    def test_available_states(self):
        from core.modem import ModemState, AVAILABLE_STATES
        assert ModemState.READY in AVAILABLE_STATES
        assert ModemState.CALL_BUSY not in AVAILABLE_STATES
        assert ModemState.SMS_BUSY not in AVAILABLE_STATES

    def test_online_states(self):
        from core.modem import ModemState, ONLINE_STATES
        assert ModemState.READY in ONLINE_STATES
        assert ModemState.OFFLINE not in ONLINE_STATES
        assert ModemState.INITIALIZING in ONLINE_STATES


class TestSingleModemProvider:
    """SingleModemProvider — state derivation from device reports."""

    @pytest.fixture
    def provider(self):
        from core.modem import SingleModemProvider
        return SingleModemProvider(modem_id="gsm", device="gsm")

    def test_initial_state_offline(self, provider):
        info = provider.get_info("gsm")
        assert info.state.value == "offline"

    def test_update_registered_becomes_ready(self, provider):
        provider.update_state("gsm", registered=True, signal_percent=85)
        info = provider.get_info("gsm")
        assert info.state.value == "ready"
        assert info.signal_percent == 85

    def test_sms_active_becomes_sms_busy(self, provider):
        provider.update_state("gsm", registered=True)
        provider.set_sms_active("gsm", True)
        info = provider.get_info("gsm")
        assert info.state.value == "sms_busy"

    def test_call_active_becomes_call_busy(self, provider):
        provider.update_state("gsm", registered=True)
        provider.set_call_active("gsm", True)
        info = provider.get_info("gsm")
        assert info.state.value == "call_busy"

    def test_error_state(self, provider):
        provider.update_state("gsm", registered=True, error="no_network")
        info = provider.get_info("gsm")
        assert info.state.value == "error"

    def test_unknown_modem_returns_none(self, provider):
        assert provider.get_info("unknown") is None

    def test_list_modems(self, provider):
        provider.update_state("gsm", registered=True)
        modems = provider.list_modems()
        assert len(modems) == 1
        assert modems[0].modem_id == "gsm"

    def test_is_available_ready(self, provider):
        provider.update_state("gsm", registered=True)
        assert provider.is_available("gsm") is True

    def test_is_available_busy(self, provider):
        provider.update_state("gsm", registered=True)
        provider.set_call_active("gsm", True)
        assert provider.is_available("gsm") is False

    def test_to_dict(self, provider):
        provider.update_state("gsm", registered=True, signal_percent=90)
        info = provider.get_info("gsm")
        d = info.to_dict()
        assert d["modem_id"] == "gsm"
        assert d["state"] == "ready"
        assert d["signal_percent"] == 90


class TestRoutingStrategies:
    """Routing strategy implementations (S05.2)."""

    def test_first_available_selects_first(self):
        from core.modem import FirstAvailableStrategy, ModemInfo, ModemState

        modems = [
            ModemInfo(modem_id="b", device="b", state=ModemState.READY),
            ModemInfo(modem_id="a", device="a", state=ModemState.READY),
        ]
        strategy = FirstAvailableStrategy()
        chosen = strategy.select(modems)
        assert chosen.modem_id == "a"  # sorted by modem_id

    def test_first_available_empty(self):
        from core.modem import FirstAvailableStrategy
        strategy = FirstAvailableStrategy()
        assert strategy.select([]) is None

    def test_round_robin_alternates(self):
        from core.modem import RoundRobinStrategy, ModemInfo, ModemState

        modems = [
            ModemInfo(modem_id="a", device="a", state=ModemState.READY),
            ModemInfo(modem_id="b", device="b", state=ModemState.READY),
        ]
        strategy = RoundRobinStrategy()
        first = strategy.select(modems)
        second = strategy.select(modems)
        third = strategy.select(modems)
        assert first.modem_id == "a"
        assert second.modem_id == "b"
        assert third.modem_id == "a"

    def test_round_robin_empty(self):
        from core.modem import RoundRobinStrategy
        strategy = RoundRobinStrategy()
        assert strategy.select([]) is None


class TestModemPool:
    """ModemPool — selection, reservation, release (S05.2)."""

    @pytest.fixture
    def pool(self):
        from core.modem import ModemPool, SingleModemProvider
        provider = SingleModemProvider(modem_id="gsm", device="gsm")
        provider.update_state("gsm", registered=True)
        return ModemPool(provider=provider)

    def test_select_for_sms(self, pool):
        chosen = pool.select_for_sms(destination="+79261234555")
        assert chosen is not None
        assert chosen.modem_id == "gsm"

    def test_select_for_call(self, pool):
        chosen = pool.select_for_call(destination="+79261234555")
        assert chosen is not None
        assert chosen.modem_id == "gsm"

    def test_release(self, pool):
        pool.select_for_call(destination="+79261234555")
        pool.release("gsm")
        assert pool.get_reserved_count() == 0

    def test_all_busy_returns_none(self, pool):
        pool.select_for_call(destination="+79261234555")
        chosen = pool.select_for_call(destination="+79261234556")
        assert chosen is None

    def test_is_all_busy(self, pool):
        assert pool.is_all_busy() is False
        pool.select_for_call(destination="+79261234555")
        assert pool.is_all_busy() is True

    def test_list_modems(self, pool):
        modems = pool.list_modems()
        assert len(modems) == 1
        assert modems[0].modem_id == "gsm"

    def test_atomic_reservation(self):
        """Two concurrent requests, one modem, exactly one winner."""
        from core.modem import ModemPool, SingleModemProvider

        provider = SingleModemProvider(modem_id="gsm", device="gsm")
        provider.update_state("gsm", registered=True)
        pool = ModemPool(provider=provider)

        results = []

        def select():
            results.append(pool.select_for_call(destination="+1111111111"))

        import threading
        t1 = threading.Thread(target=select)
        t2 = threading.Thread(target=select)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        winners = [r for r in results if r is not None]
        assert len(winners) == 1

    def test_release_allows_new_selection(self, pool):
        pool.select_for_call(destination="+79261234555")
        pool.release("gsm")
        chosen = pool.select_for_call(destination="+14155552671")
        assert chosen is not None


class TestCallRegistryWithPool:
    """CallRegistry integration with ModemPool (S05.1)."""

    @pytest.fixture
    def registry_with_pool(self):
        from core.modem import ModemPool, SingleModemProvider
        mock_sms = MagicMock()
        mock_audit = MagicMock()
        provider = SingleModemProvider(modem_id="gsm", device="gsm")
        provider.update_state("gsm", registered=True)
        pool = ModemPool(provider=provider)
        return CallRegistry(
            sms_store=mock_sms,
            audit=mock_audit,
            modem_pool=pool,
        )

    def test_outgoing_uses_pool(self, registry_with_pool):
        call = registry_with_pool.create_outgoing(
            callee_number="+14155552671",
            telegram_user_id=12345,
        )
        assert call.modem_id == "gsm"
        assert call.state == CallState.MODEM_RESERVED

    def test_outgoing_pool_busy_raises(self, registry_with_pool):
        registry_with_pool.create_outgoing(
            callee_number="+14155552671",
            telegram_user_id=12345,
        )
        with pytest.raises(ModemBusyError):
            registry_with_pool.create_outgoing(
                callee_number="+14155552672",
                telegram_user_id=12346,
            )

    def test_cleanup_releases_pool(self, registry_with_pool):
        call = registry_with_pool.create_outgoing(
            callee_number="+14155552671",
            telegram_user_id=12345,
        )
        registry_with_pool.transition(call.call_id, CallState.TELEGRAM_CALLING)
        registry_with_pool.transition(call.call_id, CallState.USER_ACCEPTED)
        registry_with_pool.transition(call.call_id, CallState.GSM_DIALING)
        registry_with_pool.transition(call.call_id, CallState.GSM_RINGING)
        registry_with_pool.transition(call.call_id, CallState.CONNECTED)
        registry_with_pool.transition(call.call_id, CallState.BRIDGED)
        registry_with_pool.transition(call.call_id, CallState.HANGUP)
        registry_with_pool.cleanup(call.call_id)
        # After cleanup, a new outgoing call should succeed
        call2 = registry_with_pool.create_outgoing(
            callee_number="+14155552672",
            telegram_user_id=12346,
        )
        assert call2.state == CallState.MODEM_RESERVED

    def test_provenance_in_dict(self, registry_with_pool):
        call = registry_with_pool.create_incoming(
            caller_number="+79261234555",
            modem_id="gsm",
        )
        d = call.to_dict()
        assert d["modem_id"] == "gsm"

    def test_backwards_compat_no_pool(self):
        """Without a pool, CallRegistry falls back to direct reservation."""
        mock_sms = MagicMock()
        mock_audit = MagicMock()
        registry = CallRegistry(
            sms_store=mock_sms,
            audit=mock_audit,
            modem_pool=None,
        )
        call = registry.create_outgoing(callee_number="+14155552671")
        assert call.state == CallState.MODEM_RESERVED
        with pytest.raises(ModemBusyError):
            registry.create_outgoing(callee_number="+14155552672")


# =========================================================================
# S05.1 — Modem abstraction and provenance
# =========================================================================

class TestModemStates:
    """ModemState enum (GPT §7.2)."""

    def test_all_states_exist(self):
        from core.modem import ModemState
        expected = {
            "offline", "initializing", "ready", "busy",
            "sms_busy", "call_busy", "error", "disabled",
        }
        actual = {s.value for s in ModemState}
        assert expected == actual

    def test_available_states(self):
        from core.modem import ModemState, AVAILABLE_STATES
        assert ModemState.READY in AVAILABLE_STATES
        assert ModemState.CALL_BUSY not in AVAILABLE_STATES
        assert ModemState.SMS_BUSY not in AVAILABLE_STATES

    def test_online_states(self):
        from core.modem import ModemState, ONLINE_STATES
        assert ModemState.READY in ONLINE_STATES
        assert ModemState.OFFLINE not in ONLINE_STATES
        assert ModemState.INITIALIZING in ONLINE_STATES


class TestSingleModemProvider:
    """SingleModemProvider — state derivation from device reports."""

    @pytest.fixture
    def provider(self):
        from core.modem import SingleModemProvider
        return SingleModemProvider(modem_id="gsm", device="gsm")

    def test_initial_state_offline(self, provider):
        info = provider.get_info("gsm")
        assert info.state.value == "offline"

    def test_update_registered_becomes_ready(self, provider):
        provider.update_state("gsm", registered=True, signal_percent=85)
        info = provider.get_info("gsm")
        assert info.state.value == "ready"
        assert info.signal_percent == 85

    def test_sms_active_becomes_sms_busy(self, provider):
        provider.update_state("gsm", registered=True)
        provider.set_sms_active("gsm", True)
        info = provider.get_info("gsm")
        assert info.state.value == "sms_busy"

    def test_call_active_becomes_call_busy(self, provider):
        provider.update_state("gsm", registered=True)
        provider.set_call_active("gsm", True)
        info = provider.get_info("gsm")
        assert info.state.value == "call_busy"

    def test_error_state(self, provider):
        provider.update_state("gsm", registered=True, error="no_network")
        info = provider.get_info("gsm")
        assert info.state.value == "error"

    def test_unknown_modem_returns_none(self, provider):
        assert provider.get_info("unknown") is None

    def test_list_modems(self, provider):
        provider.update_state("gsm", registered=True)
        modems = provider.list_modems()
        assert len(modems) == 1
        assert modems[0].modem_id == "gsm"

    def test_is_available_ready(self, provider):
        provider.update_state("gsm", registered=True)
        assert provider.is_available("gsm") is True

    def test_is_available_busy(self, provider):
        provider.update_state("gsm", registered=True)
        provider.set_call_active("gsm", True)
        assert provider.is_available("gsm") is False

    def test_to_dict(self, provider):
        provider.update_state("gsm", registered=True, signal_percent=90)
        info = provider.get_info("gsm")
        d = info.to_dict()
        assert d["modem_id"] == "gsm"
        assert d["state"] == "ready"
        assert d["signal_percent"] == 90


class TestRoutingStrategies:
    """Routing strategy implementations (S05.2)."""

    def test_first_available_selects_first(self):
        from core.modem import FirstAvailableStrategy, ModemInfo, ModemState

        modems = [
            ModemInfo(modem_id="b", device="b", state=ModemState.READY),
            ModemInfo(modem_id="a", device="a", state=ModemState.READY),
        ]
        strategy = FirstAvailableStrategy()
        chosen = strategy.select(modems)
        assert chosen.modem_id == "a"  # sorted by modem_id

    def test_first_available_empty(self):
        from core.modem import FirstAvailableStrategy
        strategy = FirstAvailableStrategy()
        assert strategy.select([]) is None

    def test_round_robin_alternates(self):
        from core.modem import RoundRobinStrategy, ModemInfo, ModemState

        modems = [
            ModemInfo(modem_id="a", device="a", state=ModemState.READY),
            ModemInfo(modem_id="b", device="b", state=ModemState.READY),
        ]
        strategy = RoundRobinStrategy()
        first = strategy.select(modems)
        second = strategy.select(modems)
        third = strategy.select(modems)
        assert first.modem_id == "a"
        assert second.modem_id == "b"
        assert third.modem_id == "a"

    def test_round_robin_empty(self):
        from core.modem import RoundRobinStrategy
        strategy = RoundRobinStrategy()
        assert strategy.select([]) is None


class TestModemPool:
    """ModemPool — selection, reservation, release (S05.2)."""

    @pytest.fixture
    def pool(self):
        from core.modem import ModemPool, SingleModemProvider
        provider = SingleModemProvider(modem_id="gsm", device="gsm")
        provider.update_state("gsm", registered=True)
        return ModemPool(provider=provider)

    def test_select_for_sms(self, pool):
        chosen = pool.select_for_sms(destination="+79261234555")
        assert chosen is not None
        assert chosen.modem_id == "gsm"

    def test_select_for_call(self, pool):
        chosen = pool.select_for_call(destination="+79261234555")
        assert chosen is not None
        assert chosen.modem_id == "gsm"

    def test_release(self, pool):
        pool.select_for_call(destination="+79261234555")
        pool.release("gsm")
        assert pool.get_reserved_count() == 0

    def test_all_busy_returns_none(self, pool):
        pool.select_for_call(destination="+79261234555")
        chosen = pool.select_for_call(destination="+79261234556")
        assert chosen is None

    def test_is_all_busy(self, pool):
        assert pool.is_all_busy() is False
        pool.select_for_call(destination="+79261234555")
        assert pool.is_all_busy() is True

    def test_list_modems(self, pool):
        modems = pool.list_modems()
        assert len(modems) == 1
        assert modems[0].modem_id == "gsm"

    def test_atomic_reservation(self):
        """Two concurrent requests, one modem, exactly one winner."""
        from core.modem import ModemPool, SingleModemProvider

        provider = SingleModemProvider(modem_id="gsm", device="gsm")
        provider.update_state("gsm", registered=True)
        pool = ModemPool(provider=provider)

        results = []

        def select():
            results.append(pool.select_for_call(destination="+1111111111"))

        import threading
        t1 = threading.Thread(target=select)
        t2 = threading.Thread(target=select)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        winners = [r for r in results if r is not None]
        assert len(winners) == 1

    def test_release_allows_new_selection(self, pool):
        pool.select_for_call(destination="+79261234555")
        pool.release("gsm")
        chosen = pool.select_for_call(destination="+14155552671")
        assert chosen is not None


class TestCallRegistryWithPool:
    """CallRegistry integration with ModemPool (S05.1)."""

    @pytest.fixture
    def registry_with_pool(self):
        from core.modem import ModemPool, SingleModemProvider
        mock_sms = MagicMock()
        mock_audit = MagicMock()
        provider = SingleModemProvider(modem_id="gsm", device="gsm")
        provider.update_state("gsm", registered=True)
        pool = ModemPool(provider=provider)
        return CallRegistry(
            sms_store=mock_sms,
            audit=mock_audit,
            modem_pool=pool,
        )

    def test_outgoing_uses_pool(self, registry_with_pool):
        call = registry_with_pool.create_outgoing(
            callee_number="+14155552671",
            telegram_user_id=12345,
        )
        assert call.modem_id == "gsm"
        assert call.state == CallState.MODEM_RESERVED

    def test_outgoing_pool_busy_raises(self, registry_with_pool):
        registry_with_pool.create_outgoing(
            callee_number="+14155552671",
            telegram_user_id=12345,
        )
        with pytest.raises(ModemBusyError):
            registry_with_pool.create_outgoing(
                callee_number="+14155552672",
                telegram_user_id=12346,
            )

    def test_cleanup_releases_pool(self, registry_with_pool):
        call = registry_with_pool.create_outgoing(
            callee_number="+14155552671",
            telegram_user_id=12345,
        )
        registry_with_pool.transition(call.call_id, CallState.TELEGRAM_CALLING)
        registry_with_pool.transition(call.call_id, CallState.USER_ACCEPTED)
        registry_with_pool.transition(call.call_id, CallState.GSM_DIALING)
        registry_with_pool.transition(call.call_id, CallState.GSM_RINGING)
        registry_with_pool.transition(call.call_id, CallState.CONNECTED)
        registry_with_pool.transition(call.call_id, CallState.BRIDGED)
        registry_with_pool.transition(call.call_id, CallState.HANGUP)
        registry_with_pool.cleanup(call.call_id)
        # After cleanup, a new outgoing call should succeed
        call2 = registry_with_pool.create_outgoing(
            callee_number="+14155552672",
            telegram_user_id=12346,
        )
        assert call2.state == CallState.MODEM_RESERVED

    def test_provenance_in_dict(self, registry_with_pool):
        call = registry_with_pool.create_incoming(
            caller_number="+79261234555",
            modem_id="gsm",
        )
        d = call.to_dict()
        assert d["modem_id"] == "gsm"

    def test_backwards_compat_no_pool(self):
        """Without a pool, CallRegistry falls back to direct reservation."""
        mock_sms = MagicMock()
        mock_audit = MagicMock()
        registry = CallRegistry(
            sms_store=mock_sms,
            audit=mock_audit,
            modem_pool=None,
        )
        call = registry.create_outgoing(callee_number="+14155552671")
        assert call.state == CallState.MODEM_RESERVED
        with pytest.raises(ModemBusyError):
            registry.create_outgoing(callee_number="+14155552672")
