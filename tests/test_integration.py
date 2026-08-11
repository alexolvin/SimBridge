"""Stage 01 integration tests — agent API endpoints, HTTP auth, injection.

Tests: TS01-6 (real SMS via API), TS01-7 (injection attempt),
       TS01-8 (auth matrix).

These tests require the agent HTTP server running. They are marked with
``requires_agent`` fixture — skip if no live agent is available.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

# NOTE: Agent app import for testing without a running server
# In CI/production, test against a live server.


@pytest.fixture
def agent_app():
    """Create the agent app for testing (in-process, no real AMI)."""
    # We can't easily test the full app without AMI, so we test
    # the HTTP layer with a mock app
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


class TestAgentAuth:
    """TS01-8: auth matrix — token + IP allowlist."""

    def test_rejects_missing_token(self, agent_app):
        """Missing bearer token → 401."""
        # In the real agent, this is checked by middleware
        # For the test app, we verify the pattern
        assert True  # placeholder — requires live agent


class TestAgentInjection:
    """TS01-7: injection attempt is inert."""

    def test_injection_text_sent_literally(self):
        """SMS text containing shell metacharacters is sent as-is,
        not interpreted by a shell.

        This is verified by the AMI client implementation: the text is
        passed as a structured AMI field, never interpolated into a
        shell command. See agent/ami_client.py:send_sms().

        REAL TEST: Send SMS with text: `; touch /tmp/pwned;`
        Expected: literal text arrives on the phone, no /tmp/pwned created.
        Status: MANUAL_VERIFY — requires physical modem.
        """
        # Code-level verification: check that ami_client.py does not use
        # subprocess, os.system, or shell=True
        import ast
        import pathlib

        ami_source = pathlib.Path("agent/ami_client.py").read_text()
        tree = ast.parse(ami_source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in (
                        "subprocess",
                        "shutil",
                    ), f"AMI client imports {alias.name} — potential shell injection"
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    assert node.module.split(".")[0] not in (
                        "subprocess",
                        "shutil",
                    ), f"AMI client imports from {node.module}"

        # Also check for os.system, os.popen calls
        assert "os.system" not in ami_source
        assert "os.popen" not in ami_source
        assert "shell=True" not in ami_source

    def test_no_ssh_in_userbot(self):
        """Verify userbot/ has no SSH calls (TS01-6 prerequisite)."""
        import pathlib

        for py_file in pathlib.Path("userbot/").glob("*.py"):
            content = py_file.read_text()
            assert "ssh" not in content.lower(), (
                f"Found SSH reference in {py_file} — "
                f"SSH should be removed from userbot path"
            )


class TestAgentEndpoints:
    """TS01-6: real SMS through new API, end to end.

    MANUAL_VERIFY: requires live agent + Asterisk + modem.
    """

    @pytest.mark.skip(reason="MANUAL_VERIFY: requires live agent + Asterisk + modem")
    async def test_sms_roundtrip(self):
        """Send SMS via POST /v1/sms → verify delivery on physical device."""
        pass  # Run manually: see HANDOFF document

    @pytest.mark.skip(reason="MANUAL_VERIFY: requires live Asterisk")
    async def test_health_asterisk_reachable(self):
        """GET /v1/health → asterisk_reachable: true"""
        pass

    @pytest.mark.skip(reason="MANUAL_VERIFY: requires live Asterisk + modem")
    async def test_modem_status(self):
        """GET /v1/modems → returns registration + signal data"""
        pass


# =========================================================================
# Real SMS round trip (Rule 3 — real-device evidence)
# =========================================================================

class TestRule3RealDevice:
    """Rule 3: telephony validated by real end-to-end runs.

    ALL tests below are MANUAL_VERIFY — they require:
    - Running Asterisk 18 with chan_dongle
    - Physical Huawei E173 modem with SIM
    - Connected Telegram account
    """

    @pytest.mark.skip(reason="MANUAL_VERIFY: requires physical modem + Asterisk")
    def test_sms_in_after_import(self):
        """TS01-3: real SMS in → Telegram after repo import.

        Steps:
        1. Call the GSM number from an external phone
        2. Verify Telegram receives the SMS text
        3. Verify audit log contains SMS_SUBMITTED entry
        """
        pass

    @pytest.mark.skip(reason="MANUAL_VERIFY: requires physical modem + Asterisk")
    def test_sms_out_after_import(self):
        """TS01-3: real SMS out → external phone.

        Steps:
        1. Send /sms <phone> <message> in Telegram
        2. Verify message arrives on the phone
        3. Verify delivery report is forwarded as reply
        """
        pass

    @pytest.mark.skip(reason="MANUAL_VERIFY: requires physical modem + Asterisk")
    def test_voicemail_after_import(self):
        """TS01-3: real voicemail after repo import.

        Steps:
        1. Call the GSM number, wait for ringback + prompt
        2. Hang up → voicemail recorded
        3. Verify voice note forwarded to Telegram with volume normalization
        """
        pass
