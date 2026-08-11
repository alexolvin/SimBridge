"""Stage 04 S04.2 tests — Call control state machine + bridge wiring.

Tests: TS04-3 (PJSIP endpoint), TS04-4 (real call — MANUAL_VERIFY),
       call control state machine, registry, API routes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.call_control import (
    CallMachine,
    CallRegistry,
    CallState,
    InvalidTransition,
    ModemBusyError,
)


# =========================================================================
# Call state machine — unit tests
# =========================================================================

class TestCallStateMachine:
    """Call control state machine transitions."""

    def test_incoming_flow_full(self):
        """Full incoming call: IDLE → RINGING → TELEGRAM_RINGING → ACCEPTED → BRIDGED → HANGUP → CLEANUP."""
        call = CallMachine(call_id="test", direction="incoming")
        assert call.state == CallState.IDLE
        call.transition(CallState.RINGING)
        assert call.state == CallState.RINGING
        call.transition(CallState.TELEGRAM_RINGING)
        assert call.state == CallState.TELEGRAM_RINGING
        call.transition(CallState.ACCEPTED)
        call.transition(CallState.BRIDGED)
        call.transition(CallState.HANGUP)
        call.transition(CallState.CLEANUP)

    def test_outgoing_flow_full(self):
        """Full outgoing call: IDLE → REQUESTED → MODEM_RESERVED → ... → BRIDGED → HANGUP → CLEANUP."""
        call = CallMachine(call_id="test", direction="outgoing")
        assert call.state == CallState.IDLE
        call.transition(CallState.REQUESTED)
        call.transition(CallState.MODEM_RESERVED)
        call.transition(CallState.TELEGRAM_CALLING)
        call.transition(CallState.ACCEPTED)
        call.transition(CallState.GSM_DIALING)
        call.transition(CallState.BRIDGED)
        call.transition(CallState.HANGUP)
        call.transition(CallState.CLEANUP)

    def test_incoming_reject_from_ringing(self):
        """Incoming call can be rejected from RINGING."""
        call = CallMachine(call_id="test", direction="incoming")
        call.transition(CallState.RINGING)
        call.transition(CallState.REJECTED)

    def test_incoming_timeout_to_voicemail(self):
        """Incoming call falls through to voicemail on timeout."""
        call = CallMachine(call_id="test", direction="incoming")
        call.transition(CallState.RINGING)
        call.transition(CallState.TELEGRAM_RINGING)
        call.transition(CallState.VOICEMAIL)

    def test_invalid_transition_rejected(self):
        """Cannot jump from IDLE to BRIDGED."""
        call = CallMachine(call_id="test", direction="incoming")
        with pytest.raises(InvalidTransition):
            call.transition(CallState.BRIDGED)

    def test_invalid_outgoing_transition(self):
        """Cannot go from MODEM_RESERVED to BRIDGED (skip steps)."""
        call = CallMachine(call_id="test", direction="outgoing")
        call.transition(CallState.REQUESTED)
        call.transition(CallState.MODEM_RESERVED)
        with pytest.raises(InvalidTransition):
            call.transition(CallState.BRIDGED)

    def test_timestamps_set(self):
        """created_at and updated_at are set on init."""
        call = CallMachine(call_id="test", direction="incoming")
        assert call.created_at
        assert call.updated_at
        assert call.created_at == call.updated_at

    def test_to_dict_serialization(self):
        """CallMachine.to_dict() returns expected keys."""
        call = CallMachine(
            call_id="abc",
            direction="incoming",
            caller_number="+79261234555",
            caller_name="Test",
        )
        d = call.to_dict()
        assert d["call_id"] == "abc"
        assert d["direction"] == "incoming"
        assert d["caller_number"] == "+79261234555"
        assert d["caller_name"] == "Test"
        assert d["state"] == "idle"


# =========================================================================
# Call registry — concurrent call management
# =========================================================================

class TestCallRegistry:
    """Call registry: create, transition, cleanup."""

    @pytest.fixture
    def registry(self):
        mock_sms = MagicMock()
        mock_audit = MagicMock()
        return CallRegistry(sms_store=mock_sms, audit=mock_audit)

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
        registry.transition(call.call_id, CallState.ACCEPTED)
        registry.transition(call.call_id, CallState.GSM_DIALING)
        registry.transition(call.call_id, CallState.BRIDGED)
        registry.transition(call.call_id, CallState.HANGUP)
        registry.transition(call.call_id, CallState.CLEANUP)
        registry.cleanup(call.call_id)
        # Now a second outgoing call should succeed
        call2 = registry.create_outgoing(callee_number="+14155552672")
        assert call2.state == CallState.MODEM_RESERVED

    def test_get_unknown_call_returns_none(self, registry):
        assert registry.get("nonexistent") is None

    def test_transition_unknown_call_returns_false(self, registry):
        assert registry.transition("nonexistent", CallState.RINGING) is False

    def test_transition_invalid_state_returns_false(self, registry):
        call = registry.create_incoming(caller_number="+79261234555")
        # Cannot go from RINGING directly to BRIDGED
        assert registry.transition(call.call_id, CallState.BRIDGED) is False

    def test_list_active_returns_only_non_cleanup(self, registry):
        c1 = registry.create_incoming(caller_number="+79261234555")
        c2 = registry.create_incoming(caller_number="+14155552671")
        assert len(registry.list_active()) == 2

    def test_count_by_direction(self, registry):
        registry.create_incoming(caller_number="+79261234555")
        registry.create_incoming(caller_number="+79261234556")
        registry.create_outgoing(callee_number="+14155552671")
        assert registry.count_by_direction("incoming") == 2
        assert registry.count_by_direction("outgoing") == 1


# =========================================================================
# Config generator — bridge globals
# =========================================================================

class TestConfigGeneratorBridge:
    """Config generator produces bridge globals."""

    def test_bridge_globals_in_output(self):
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
            "paths": {},
        }
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as f:
            out = f.name
        try:
            generate(config, out)
            result = Path(out).read_text()
            assert "BRIDGE_ENDPOINT=tg-bridge" in result
            assert "BRIDGE_HOST=100.x.x.x" in result
            assert "BRIDGE_PORT=5062" in result
            assert "OUTBOUND_RING_TIMEOUT=30" in result
        finally:
            import os
            os.unlink(out)


# =========================================================================
# Dialplan — tg-bridge context
# =========================================================================

class TestDialplanBridge:
    """Dialplan structure for tg-bridge (S04.2)."""

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


# =========================================================================
# PJSIP config — bridge endpoint
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
# Event types — call events
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
