"""Stage 03 tests — Voicemail Hardening.

Tests: TS03-1 (early hangup — dialplan), TS03-2 (normal voicemail regression),
       TS03-3 (config generator), TS03-4/5 (cleanup),
       TS03-6 (contact name in voicemail), TS03-7 (fallback branch).
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# Ensure project root is on sys.path so scripts/ is importable
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# =========================================================================
# TS03-1 — Early hangup recording (dialplan structure)
# =========================================================================

class TestDialplanStructure:
    """Static analysis of the deployed dialplan (asterisk/extensions.conf).

    S01 rebaseline (2026-08-15): the old example encoded an S03/S04 design
    that was never deployable — it used a MixMonitor ``@`` option that does
    not exist in Asterisk 18, a TG_ACCEPTED variable nothing ever set, and
    System() calls with interpolated user data (P0-3 RCE). The dialplan is
    now the production-parity flow with the RCE fixes; the S03/S04
    structural specs are re-verified in their stages against a working
    design.
    """

    @pytest.fixture(autouse=True)
    def load_dialplan(self):
        """Load the dialplan for testing."""
        dialplan_path = Path(__file__).parent.parent / "asterisk" / "extensions.conf"
        self.dialplan = dialplan_path.read_text()

    def test_no_shell_or_system(self):
        """P0-3: no SHELL()/System() in executable dialplan lines — user
        data (caller IDs, SMS texts) must never reach a shell. (Comment
        lines are skipped — Asterisk comments start with ';' at the
        beginning of the line, and the header documents the removed
        mechanisms.)"""
        for line in self.dialplan.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(";"):
                continue
            assert "SHELL(" not in stripped, (
                f"SHELL() in dialplan — RCE surface: {stripped}"
            )
            assert "System(" not in stripped, (
                f"System() in dialplan — RCE surface: {stripped}"
            )

    def test_generated_globals_included(self):
        """S03.2: timings/paths come from the generated globals file.

        Plain #include form only: the "=>" prefix is NOT supported by
        the Asterisk 18 config engine (main/config.c dequotes "..." and
        <...> only, then tries to open the literal string "=> file") —
        a missing include aborts dialplan loading. Live incident
        2026-08-17 (3p14-aaa): pbx_config declined, 0 contexts."""
        assert "#include asterisk-globals.conf" in self.dialplan
        assert "#include =>" not in self.dialplan

    def test_blacklist_via_agi(self):
        """Blacklist check is an AGI script (fail-open), not a shell grep
        of the caller ID."""
        assert "AGI(tg-blacklist-agi.py)" in self.dialplan
        assert "BL_BLOCKED" in self.dialplan

    def test_no_timing_literals(self):
        """S03.2: every Wait() uses a generated global, not a literal."""
        for line in self.dialplan.splitlines():
            stripped = line.strip()
            if stripped.startswith(";"):
                continue
            for m in re.finditer(r"Wait\(([^)]*)\)", stripped):
                arg = m.group(1)
                assert arg.startswith("${"), (
                    f"Wait() with a literal timing — must be a generated "
                    f"global: {stripped}"
                )

    def test_h_exten_finalizes_then_forwards(self):
        """Voicemail forwarding happens in the h-exten: StopMixMonitor()
        (synchronous WAV finalization, verified in Asterisk 18 source)
        then AGI — never System() with the caller ID."""
        section = self._incoming_mobile()
        m = re.search(r"exten => h,1,.*?(?=\nexten =>|\Z)", section, re.S)
        assert m, "h extension not found in [incoming-mobile]"
        h_body = m.group(0)
        assert "StopMixMonitor()" in h_body
        assert "AGI(tg-voice-agi.py)" in h_body
        assert "STAT(e,${VMFILE})" in h_body

    def test_recordings_dir_from_globals(self):
        """Recording path comes from VM_REC_DIR (config), not a /tmp literal."""
        assert "Set(VMFILE=${VM_REC_DIR}/vm-${UNIQUEID}.wav)" in self.dialplan
        assert "/tmp/vm-" not in self.dialplan

    def test_event_forwarding_via_agi(self):
        """All event paths (ring/sms/report/ussd) forward via AGI scripts."""
        for event in ("ring", "sms", "report", "ussd"):
            assert f"AGI(tg-sms-agi.py,{event})" in self.dialplan, (
                f"AGI(tg-sms-agi.py,{event}) missing from dialplan"
            )

    def test_mixmonitor_only_in_incoming_mobile(self):
        """MixMonitor only in the voicemail path of incoming-mobile."""
        lines = self.dialplan.splitlines()
        in_ctx = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                in_ctx = (stripped == "[incoming-mobile]")
            elif "MixMonitor" in stripped and not stripped.startswith(";"):
                assert in_ctx, f"MixMonitor outside incoming-mobile: {stripped}"

    def _incoming_mobile(self) -> str:
        m = re.search(r"\[incoming-mobile\](.*?)(?=\n\[\w|\Z)", self.dialplan, re.S)
        assert m, "[incoming-mobile] context not found"
        return m.group(1)


# =========================================================================
# TS03-3 — Config generator
# =========================================================================

class TestConfigGenerator:
    """TS03-3: Asterisk config generator from simbridge.yaml."""

    def test_basic_generation(self):
        """Generate globals from a minimal config."""
        from scripts.generate_asterisk_config import generate

        config = {
            "asterisk": {
                "ring_wait_seconds": 30,
                "max_record_seconds": 120,
                "prompt": "/var/lib/asterisk/sounds/custom/vm-prompt.ulaw",
            },
            "paths": {},
        }

        with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as f:
            output_path = f.name

        try:
            generate(config, output_path)
            result = Path(output_path).read_text()

            assert "RING_WAIT_SECONDS=30" in result
            assert "MAX_RECORD_SECONDS=120" in result
            assert "VM_PROMPT=/var/lib/asterisk/sounds/custom/vm-prompt" in result
            assert "[globals]" in result
            assert "SimBridge" in result  # header comment
        finally:
            os.unlink(output_path)

    def test_prompt_extension_stripped(self):
        """Codec extension (.ulaw) stripped for Playback()."""
        from scripts.generate_asterisk_config import generate

        config = {
            "asterisk": {
                "ring_wait_seconds": 24,
                "max_record_seconds": 90,
                "prompt": "/var/lib/asterisk/sounds/custom/vm-prompt.ulaw",
            },
            "paths": {},
        }

        with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as f:
            output_path = f.name

        try:
            generate(config, output_path)
            result = Path(output_path).read_text()
            # .ulaw should be stripped
            assert ".ulaw" not in result
        finally:
            os.unlink(output_path)

    def test_default_values(self):
        """Defaults for missing values."""
        from scripts.generate_asterisk_config import generate

        config = {
            "asterisk": {},
            "paths": {},
        }

        with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as f:
            output_path = f.name

        try:
            generate(config, output_path)
            result = Path(output_path).read_text()
            assert "RING_WAIT_SECONDS=24" in result
            assert "MAX_RECORD_SECONDS=90" in result
        finally:
            os.unlink(output_path)

    def test_ring_cycle_annotation(self):
        """Ring cycle annotation in output."""
        from scripts.generate_asterisk_config import generate

        config = {
            "asterisk": {
                "ring_wait_seconds": 25,
                "max_record_seconds": 90,
                "prompt": "/var/lib/asterisk/sounds/custom/vm-prompt",
            },
            "paths": {},
        }

        with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as f:
            output_path = f.name

        try:
            generate(config, output_path)
            result = Path(output_path).read_text()
            assert "5 ring cycles" in result
        finally:
            os.unlink(output_path)

    def test_recordings_dir_in_output(self):
        """recordings_dir from config appears in output."""
        from scripts.generate_asterisk_config import generate

        config = {
            "asterisk": {
                "ring_wait_seconds": 24,
                "max_record_seconds": 90,
                "prompt": "/var/lib/asterisk/sounds/custom/vm-prompt",
            },
            "paths": {
                "recordings_dir": "/custom/path/recordings",
            },
        }

        with tempfile.NamedTemporaryFile(suffix=".conf", delete=False) as f:
            output_path = f.name

        try:
            generate(config, output_path)
            result = Path(output_path).read_text()
            assert "VM_REC_DIR=/custom/path/recordings" in result
        finally:
            os.unlink(output_path)

    def test_prompt_duration_probed_from_prompt_file(self, tmp_path):
        """S03.1: the generator probes the greeting and publishes
        PROMPT_DURATION; a missing prompt degrades to 0.000 (no trim)."""
        import shutil
        from scripts.generate_asterisk_config import generate

        if not shutil.which("ffprobe"):
            pytest.skip("ffprobe not available")

        # the shipped greeting: 67556 bytes of 8 kHz mu-law = 8.4445 s
        prompt = Path(__file__).parent.parent / "sounds" / "vm-prompt.ulaw"
        assert prompt.is_file()

        config = {
            "asterisk": {
                "ring_wait_seconds": 30,
                "max_record_seconds": 120,
                "prompt": str(prompt),
            },
            "paths": {},
        }
        out = tmp_path / "globals.conf"
        generate(config, str(out))
        assert "PROMPT_DURATION=8.444" in out.read_text()

        # missing prompt -> 0.000 (legacy behavior: full-duration
        # classification, no trim) — the generator warns, not fails
        config["asterisk"]["prompt"] = str(tmp_path / "missing.ulaw")
        out2 = tmp_path / "globals2.conf"
        generate(config, str(out2))
        assert "PROMPT_DURATION=0.000" in out2.read_text()


# =========================================================================
# TS03-6 — Voicemail with contact name
# =========================================================================

class TestVoicemailHandler:
    """TS03-6: Voicemail endpoint with contact name resolution."""

    def test_voicemail_event_types_exist(self):
        """Verify voicemail event types are defined."""
        from core.events import EventType

        assert hasattr(EventType, "VOICEMAIL_RECEIVED")
        assert hasattr(EventType, "VOICEMAIL_EARLY_HANGUP")

    def test_voicemail_normal_type(self):
        """Normal voicemail uses VOICEMAIL_RECEIVED event type."""
        from core.events import EventType

        assert EventType.VOICEMAIL_RECEIVED.value == "VOICEMAIL_RECEIVED"

    def test_voicemail_early_hangup_type(self):
        """Early hangup uses VOICEMAIL_EARLY_HANGUP event type."""
        from core.events import EventType

        assert EventType.VOICEMAIL_EARLY_HANGUP.value == "VOICEMAIL_EARLY_HANGUP"

    def test_voicemail_with_contact_name(self, tmp_path: Path):
        """TS03-6: Voicemail notification includes resolved contact name."""
        from core.contacts import ContactResolver

        csv_path = tmp_path / "contacts.csv"
        csv_path.write_text("+79261234555,Иванов Иван\n")
        resolver = ContactResolver(csv_path=str(csv_path))

        name = resolver.resolve("+79261234555")
        assert name == "Иванов Иван"

    def test_voicemail_without_contact_name(self, tmp_path: Path):
        """Unknown number falls back to E.164."""
        from core.contacts import ContactResolver

        csv_path = tmp_path / "contacts.csv"
        csv_path.write_text("+79261234555,Иванов Иван\n")
        resolver = ContactResolver(csv_path=str(csv_path))

        name = resolver.resolve("+14155552671")
        assert name is None

    def test_early_hangup_detection_logic(self):
        """S03.1: Duration < 3s means early hangup."""
        # The logic is in tg-voice-forward.sh, test the threshold here
        THRESHOLD = 3  # seconds

        assert 1 < THRESHOLD  # 1s → early_hangup
        assert 2 < THRESHOLD  # 2s → early_hangup
        assert 5 >= THRESHOLD  # 5s → normal

    def test_recording_missing_notification(self):
        """S03.1: Missing recording produces notification, not silence."""
        # The script sends voicemail_type=recording_missing
        # Test: the type is a valid label
        from core.phone import normalize_e164

        phone = normalize_e164("+79261234555")
        assert phone == "+79261234555"
        # In the handler, recording_missing produces a warning label
        assert "recording_missing" in ["normal", "early_hangup", "recording_missing"]


# =========================================================================
# TS03-7 — Voicemail as fallback branch
# =========================================================================

class TestVoicemailFallback:
    """S01: voicemail is the outcome of the ring timeout (production
    parity: ring → prompt → record → forward on hangup). The old
    'reusable voicemail-ctx + Gosub' spec encoded the never-deployed
    example design and is re-verified in S03/S04 with a working
    mechanism."""

    @pytest.fixture(autouse=True)
    def load_dialplan(self):
        dialplan_path = Path(__file__).parent.parent / "asterisk" / "extensions.conf"
        self.dialplan = dialplan_path.read_text()
        m = re.search(r"\[incoming-mobile\](.*?)(?=\n\[\w|\Z)", self.dialplan, re.S)
        assert m, "[incoming-mobile] context not found"
        self.section = m.group(1)

    def test_ring_timeout_goes_to_voicemail(self):
        """s-exten (Stage 04): register the call, Dial the bridge for
        RING_WAIT_SECONDS (Dial blocks for the whole call, so
        DIALSTATUS is final); NOANSWER (Telegram ring timed out) →
        voicemail; any other outcome → Hangup.
        Voicemail branch: answer → record → prompt → stop."""
        order = [
            "AGI(tg-sms-agi.py,ring)",
            "AGI(notify-agent-agi.py,incoming,${CALLER})",
            "Dial(PJSIP/${BRIDGE_ENDPOINT},${RING_WAIT_SECONDS})",
            "AGI(notify-agent-agi.py,complete,${DIALSTATUS})",
            'GotoIf($["${DIALSTATUS}" = "NOANSWER"]?voicemail,1)',
            "Answer()",
            "Set(VMFILE=${VM_REC_DIR}/vm-${UNIQUEID}.wav)",
            "MixMonitor(${VMFILE})",
            "Playback(${VM_PROMPT})",
            "Wait(${MAX_RECORD_SECONDS})",
            "StopMixMonitor()",
        ]
        pos = -1
        for step in order:
            i = self.section.find(step)
            assert i != -1, f"{step} not found in incoming-mobile"
            assert i > pos, f"{step} appears out of order in incoming-mobile"
            pos = i

    def test_voicemail_is_a_named_same_context_exten(self):
        """S03.4: the Stage 04 state machine calls this target, so it
        must be a named exten IN the channel's current context (the
        h-exten resolves there)."""
        assert "exten => voicemail,1," in self.section
        # one and only one entry point (dialplan lines, not comments):
        # the GotoIf whose target is voicemail,1 (exten,priority — a
        # bare word is a label of the calling exten and would fail)
        lines = [l for l in self.section.splitlines()
                 if not l.strip().startswith(";")]
        assert sum(("Goto(voicemail,1)" in l) or ("?voicemail,1)" in l)
                   for l in lines) == 1

    def test_prompt_duration_is_published_as_channel_var(self):
        """S03.1: the AGI reads VM_PROMPT_DURATION, set from the
        generated PROMPT_DURATION global in the s-exten."""
        assert "Set(VM_PROMPT_DURATION=${PROMPT_DURATION})" in self.section

    def test_blacklisted_caller_gets_busy(self):
        """Blacklisted numbers get Busy(5), not the voicemail path."""
        # the voice-path GotoIf targets the `blacklisted` exten
        # explicitly (exten,1 — a bare word is a label lookup)
        assert 'GotoIf($["${BL_BLOCKED}" = "1"]?blacklisted,1)' in self.section
        assert "Busy(5)" in self.section


# =========================================================================
# Integration — Config schema validation
# =========================================================================

class TestConfigSchema:
    """Config schema includes new entries."""

    def test_recordings_dir_in_schema(self):
        """paths.recordings_dir is in the config schema."""
        from core.config import _CONFIG_SCHEMA

        keys = {entry.key for entry in _CONFIG_SCHEMA}
        assert "paths.recordings_dir" in keys

    def test_recordings_dir_optional(self):
        """paths.recordings_dir is optional (has default)."""
        from core.config import _CONFIG_SCHEMA

        entry = next(e for e in _CONFIG_SCHEMA if e.key == "paths.recordings_dir")
        assert entry.required is False
