"""Stage 04 S04.1 tests — Bridge selection validation & PJSIP config.

Tests: TS04-1 (selection documented), TS04-2 (real call — MANUAL_VERIFY).
"""

from __future__ import annotations

from pathlib import Path

import pytest


# =========================================================================
# TS04-1 — Bridge selection documented
# =========================================================================

class TestBridgeSelection:
    """TS04-1: Bridge selection is documented in voice-bridge.md."""

    @pytest.fixture(autouse=True)
    def load_doc(self):
        doc_path = Path(__file__).parent.parent / "docs" / "voice-bridge.md"
        self.doc = doc_path.read_text()

    def test_selection_documented(self):
        """Selection decision is recorded."""
        assert "Selection:" in self.doc or "SELECTED" in self.doc

    def test_primary_candidate_disqualified_noted(self):
        """Infactum/tg2sip disqualified for libtgvoip."""
        assert "libtgvoip" in self.doc
        assert "DISQUALIFIED" in self.doc

    def test_selected_candidate_uses_ntgcalls(self):
        """Selected candidate uses ntgcalls, not libtgvoip."""
        assert "ntgcalls" in self.doc
        assert "sip-tg-bridge" in self.doc

    def test_transport_decision_plain_rtp(self):
        """Transport: plain RTP over Tailscale, no SRTP."""
        assert "Plain RTP" in self.doc
        assert "SRTP" in self.doc

    def test_pjsip_config_direct_media_off(self):
        """PJSIP config: direct_media=no (required for bridging)."""
        assert "direct_media=no" in self.doc

    def test_pjsip_config_codec_ulaw_alaw(self):
        """PJSIP config: only ulaw/alaw (GSM-compatible)."""
        assert "allow=ulaw,alaw" in self.doc

    def test_pjsip_config_dtmf_rfc2833(self):
        """PJSIP config: DTMF via RFC2833."""
        assert "dtmf_mode=rfc2833" in self.doc

    def test_voicemail_fallback_documented(self):
        """S03.4: voicemail is a named, same-context fallback branch that
        the Stage 04 state machine calls (not a separate voicemail-ctx)."""
        assert "named, same-context branch" in self.doc
        assert "Goto(voicemail, 1)" in self.doc
        assert "unchanged fallback target" in self.doc
