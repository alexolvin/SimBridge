"""Stage 01 tests — foundation: secret detection, config validation, core modules.

Tests: TS01-1 (sanitization grep), TS01-2 (blocked fake-secret commit),
       TS01-4 (config validation), TS01-9 (ACL deny), TS01-11 (rate limit).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from core.config import load_config, ConfigError, DotDict
from core.audit import AuditLogger
from core.acl import ACLManager
from core.ratelimit import RateLimiter
from core.secrets_check import scan_file


# =========================================================================
# TS01-1 — Secret detection: sanitization grep
# =========================================================================

class TestSecretDetection:
    """TS01-1: secrets_check.py detects all pattern classes."""

    def _write_and_scan(self, content: str) -> list:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            f.flush()
            return scan_file(f.name)

    def test_detects_telegram_api_hash(self):
        matches = self._write_and_scan(
            'API_HASH = "0123456789abcdef0123456789abcdef"'
        )
        assert any(m.pattern_name == "telegram_api_hash" for m in matches)

    def test_detects_telegram_api_id(self):
        matches = self._write_and_scan(
            "API_ID = 12345678"
        )
        assert any(m.pattern_name == "telegram_api_id" for m in matches)

    def test_detects_e164_phone(self):
        matches = self._write_and_scan(
            'phone = "+79161234567"'
        )
        assert any(m.pattern_name == "e164_phone" for m in matches)

    def test_detects_session_file_ref(self):
        matches = self._write_and_scan(
            'session = "my_bot.session"'
        )
        assert any(m.pattern_name == "session_file_ref" for m in matches)

    def test_detects_http_secret(self):
        matches = self._write_and_scan(
            'HTTP_SECRET = "abcdef0123456789abcdef0123456789"'
        )
        assert any(m.pattern_name == "http_secret" for m in matches)

    def test_no_false_positive_on_placeholders(self):
        """Placeholder text should NOT trigger detection."""
        matches = self._write_and_scan(
            "# Replace with your actual API_HASH from my.telegram.org\n"
            "API_HASH = os.environ['SIMBRIDGE_TG_API_HASH']"
        )
        assert not matches, f"Unexpected matches: {matches}"


# =========================================================================
# TS01-2 — Pre-commit hook blocks fake secrets
# =========================================================================

class TestPreCommitHook:
    """TS01-2: deliberate fake-secret commit shown blocked."""

    def test_hook_blocks_secret_commit(self):
        """Simulate the pre-commit hook scanning a file with a secret."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('API_HASH = "0123456789abcdef0123456789abcdef"\n')
            f.flush()
            matches = scan_file(f.name)
        assert matches, "Expected secret detection, got none"

    def test_hook_allows_clean_file(self):
        """A file with no secrets should pass."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("x = 1 + 2\nprint(x)\n")
            f.flush()
            matches = scan_file(f.name)
        assert not matches, f"False positive: {matches}"


# =========================================================================
# TS01-4 — Config validation: three failure cases
# =========================================================================

class TestConfigValidation:
    """TS01-4: unknown key, missing key, missing secret — three failures."""

    def test_missing_required_key(self, tmp_path: Path):
        """Config with missing 'node.role' should fail."""
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text("node:\n  id: test\n")  # missing role
        with pytest.raises(ConfigError, match="Missing required key"):
            load_config(str(cfg_path))

    def test_unknown_key(self, tmp_path: Path):
        """Config with unknown top-level key should fail."""
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text("""\
node:
  role: gsm
  id: test
bogus_key: true
""")
        with pytest.raises(ConfigError, match="Unknown key"):
            load_config(str(cfg_path))

    def test_bad_type(self, tmp_path: Path):
        """Config with wrong type for sms_per_hour should fail."""
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text("""\
node:
  role: gsm
  id: test
limits:
  sms_per_hour: "thirty"
  calls_per_minute: 3
  max_call_seconds: 3600
""")
        with pytest.raises(ConfigError, match="expected int"):
            load_config(str(cfg_path))

    def test_valid_config_loads(self, tmp_config: str):
        """A valid config loads without error."""
        cfg = load_config(tmp_config)
        assert cfg["node.role"] == "all-in-one"
        assert cfg["agent.listen"] == "127.0.0.1:8090"

    def test_missing_secret_env(self, tmp_path: Path):
        """Config that references an unset env var should fail."""
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text("""\
node:
  role: gsm
  id: test
telegram:
  master_username: test
  session_path: /tmp/t
  acl_file: /tmp/a
  api_id_env: SIMBRIDGE_TG_API_ID
  api_hash_env: SIMBRIDGE_TG_API_HASH
agent:
  listen: "127.0.0.1:8090"
  token_env: NONEXISTENT_ENV_VAR
  allowed_peers: []
userbot_http:
  listen: "127.0.0.1:8088"
  secret_env: SIMBRIDGE_HTTP_SECRET
  allowed_peers: []
asterisk:
  ari_url: http://127.0.0.1:8088/ari
  dongle: gsm
  ring_wait_seconds: 24
  max_record_seconds: 90
  prompt: /tmp/p
voice:
  bridge_endpoint: tg-bridge
  bridge_host: 127.0.0.1
  bridge_port: 5062
  srtp: false
  outbound_answer_timeout: 30
limits:
  sms_per_hour: 30
  calls_per_minute: 3
  max_call_seconds: 3600
paths:
  blacklist: /tmp/b
  contacts_cache: /tmp/c
  audit_log: /tmp/a
""")
        # Make sure the env var is NOT set
        os.environ.pop("NONEXISTENT_ENV_VAR", None)
        # Set the ones that DO exist
        os.environ["SIMBRIDGE_TG_API_ID"] = "12345"
        os.environ["SIMBRIDGE_TG_API_HASH"] = "0123456789abcdef0123456789abcdef"
        os.environ["SIMBRIDGE_HTTP_SECRET"] = "test"

        with pytest.raises(ConfigError, match="NONEXISTENT_ENV_VAR"):
            load_config(str(cfg_path))


# =========================================================================
# TS01-9 — ACL default deny
# =========================================================================

class TestACL:
    """TS01-9: unknown Telegram ID → denied + audit record."""

    def test_default_deny(self, tmp_path: Path):
        acl_file = tmp_path / "acl.conf"
        acl_file.write_text("1234567 out_sms\n")
        acl = ACLManager(str(acl_file))

        assert acl.check(9999999, "out_sms") is False  # unknown user
        assert acl.check(1234567, "out_sms") is True  # known user

    def test_reload(self, tmp_path: Path):
        acl_file = tmp_path / "acl.conf"
        acl_file.write_text("1234567 out_sms\n")
        acl = ACLManager(str(acl_file))

        # Add new user — force mtime change so reload() detects it
        import os, time
        time.sleep(0.01)
        acl_file.write_text("1234567 out_sms\n9999999 in_sms\n")
        # Touch to ensure mtime updates on fast filesystems
        os.utime(str(acl_file))
        acl.reload()

        assert acl.check(9999999, "in_sms") is True


# =========================================================================
# TS01-11 — Rate limiter
# =========================================================================

class TestRateLimiter:
    """TS01-11: exceed sms_per_hour → refused."""

    def test_allows_within_limit(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        assert limiter.allow("user1") is True
        assert limiter.allow("user1") is True
        assert limiter.allow("user1") is True
        assert limiter.allow("user1") is False  # 4th — denied

    def test_per_key_isolation(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        assert limiter.allow("user1") is True
        assert limiter.allow("user1") is True
        assert limiter.allow("user1") is False  # user1 exceeded

        # user2 is independent
        assert limiter.allow("user2") is True

    def test_remaining(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        assert limiter.remaining("user1") == 5
        limiter.allow("user1")
        assert limiter.remaining("user1") == 4


# =========================================================================
# Audit logger
# =========================================================================

class TestAuditLogger:
    def test_append_records(self, tmp_path: Path):
        log_path = tmp_path / "audit.jsonl"
        logger = AuditLogger(str(log_path))

        logger.log("SMS_SEND_REQUESTED", telegram_user_id=123, outcome="ok")
        logger.log("USER_DENIED", telegram_user_id=999, outcome="denied")

        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert '"event": "SMS_SEND_REQUESTED"' in lines[0]
        assert '"event": "USER_DENIED"' in lines[1]

    def test_utc_timestamps(self, tmp_path: Path):
        log_path = tmp_path / "audit.jsonl"
        logger = AuditLogger(str(log_path))
        logger.log("CONFIG_RELOADED")

        import json
        record = json.loads(log_path.read_text().strip())
        assert "T" in record["timestamp"]  # ISO-8601
        assert "+00:00" in record["timestamp"] or "Z" in record["timestamp"]
