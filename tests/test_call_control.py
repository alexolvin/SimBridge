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

class TestDialplanBridge:
    """Dialplan structure for tg-bridge (S04.2) and S04.3 contexts."""

    @pytest.fixture(autouse=True)
    def load_dialplan(self):
        dp = Path(__file__).parent.parent / "asterisk" / "extensions.conf.example"
        self.dialplan = dp.read_text()

    def test_tg_bridge_context_exists(self):
        """[tg-bridge] context exists for bridge inbound calls."""
        assert "[tg-bridge]" in self.dialplan

    def test_tg_bridge_has_route(self):
        """tg-bridge context has a routing extension."""
        lines = self.dialplan.split("\n")
        in_ctx = False
        for line in lines:
            if "[tg-bridge]" in line:
                in_ctx = True
            elif line.strip().startswith("[") and in_ctx:
                break
            if in_ctx and "exten" in line and not line.strip().startswith(";"):
                return
        pytest.fail("No extension found in [tg-bridge] context")

    def test_tg_bridge_sip_context_exists(self):
        """[tg-bridge-sip] context exists for outgoing GSM dial (S04.3)."""
        assert "[tg-bridge-sip]" in self.dialplan

    def test_incoming_mobile_telegram_flow(self):
        """incoming-mobile context uses TG_ACCEPTED variable for S04.3 flow."""
        assert "TG_ACCEPTED" in self.dialplan

    def test_incoming_mobile_notifies_agent(self):
        """incoming-mobile notifies agent via AGI."""
        assert "notify-agent-agi" in self.dialplan

    def test_tg_bridge_sip_dials_dongle(self):
        """tg-bridge-sip context dials via Dongle."""
        lines = self.dialplan.split("\n")
        in_ctx = False
        for line in lines:
            if "[tg-bridge-sip]" in line:
                in_ctx = True
            elif line.strip().startswith("[") and in_ctx:
                break
            if in_ctx and "Dongle" in line and not line.strip().startswith(";"):
                return
        pytest.fail("No Dongle dial found in [tg-bridge-sip] context")


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
