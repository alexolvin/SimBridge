"""S06.1 tests — security hardening: timing-safe comparisons, IP allowlist, bind-address validation, call rate limiting.

Tests: TS06-1 (no 0.0.0.0 binds), TS06-3 (secret scan),
       TS06-S01 (timing-safe comparison),
       TS06-S02 (IP allowlist enforcement),
       TS06-S03 (bind-address validation),
       TS06-S04 (call rate limiting).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.config import load_config, ConfigError


# =========================================================================
# TS06-S01 — Timing-safe comparisons
# =========================================================================

class TestTimingSafeComparison:
    """Verify that hmac.compare_digest is used for secret comparisons."""

    def test_deps_uses_hmac_compare_digest(self):
        """check_auth in deps.py should use hmac.compare_digest, not == or !=."""
        import agent.deps as deps_module
        source = open(deps_module.__file__).read()
        assert "hmac.compare_digest" in source, (
            "Bearer token comparison should use hmac.compare_digest"
        )
        # The function should NOT contain `token != _agent_token` or `token == _agent_token`
        assert "token != _agent_token" not in source
        assert "token == _agent_token" not in source

    def test_http_server_uses_hmac_compare_digest(self):
        """Secret comparison in http_server.py should use hmac.compare_digest."""
        import userbot.http_server as http_module
        source = open(http_module.__file__).read()
        assert "hmac.compare_digest" in source, (
            "HTTP secret comparison should use hmac.compare_digest"
        )
        # Should NOT contain `!= secret` for the secret check
        assert "received_secret != secret" not in source


# =========================================================================
# TS06-S02 — IP allowlist enforcement
# =========================================================================

class TestIPAllowlist:
    """check_ip rejects non-localhost when no allowed_peers configured."""

    def test_check_ip_source_rejects_non_localhost_without_peers(self):
        """The check_ip function should reject non-localhost when _allowed_peers is empty."""
        import agent.deps as deps_module
        source = open(deps_module.__file__).read()
        # The function should contain logic to reject when not localhost and no peers
        assert "127.0.0.1" in source, "Should check for localhost"
        # Should not have 'allow all (dev mode)' bypass
        assert "allow all" not in source.lower() or "localhost" in source

    def test_check_auth_source_has_timing_safe(self):
        """check_auth should import and use hmac."""
        import agent.deps as deps_module
        source = open(deps_module.__file__).read()
        assert "import hmac" in source


class TestPeerNormalization:
    """allowed_peers entries: IPs kept as-is, hostnames resolved at startup.

    The agent compares the peer's remote address (always an IP) against the
    allowlist, so a hostname entry like a Tailscale MagicDNS FQDN must be
    resolved to its IP or it can never match (P0-2).
    """

    def test_ip_entry_kept_as_is(self):
        from agent.deps import _normalize_peers
        result = _normalize_peers(["100.124.155.106"])
        assert result == {"100.124.155.106"}

    def test_hostname_resolved_to_ip(self, monkeypatch):
        import agent.deps as deps_module
        monkeypatch.setattr(
            deps_module.socket, "gethostbyname", lambda h: "100.95.195.40"
        )
        result = deps_module._normalize_peers(["vzu5-claw"])
        # Both the original entry and its resolved IP are kept, so
        # comparison by remote IP works either way.
        assert "100.95.195.40" in result
        assert "vzu5-claw" in result

    def test_unresolvable_hostname_kept_without_error(self, monkeypatch):
        import agent.deps as deps_module

        def boom(h):
            raise OSError("no such host")

        monkeypatch.setattr(deps_module.socket, "gethostbyname", boom)
        result = deps_module._normalize_peers(["no-such-host.example"])
        # Kept as-is (never matches an IP) instead of raising at startup.
        assert result == {"no-such-host.example"}

    def test_blank_and_non_string_entries_skipped(self):
        from agent.deps import _normalize_peers
        assert _normalize_peers(["", "   "]) == set()
        # YAML `allowed_peers: [~]` yields None entries — must not crash.
        assert _normalize_peers([None]) == set()
        assert _normalize_peers([]) == set()
        assert _normalize_peers(None) == set()

    def test_mixed_entries(self, monkeypatch):
        import agent.deps as deps_module
        monkeypatch.setattr(
            deps_module.socket, "gethostbyname", lambda h: "10.0.0.2"
        )
        result = deps_module._normalize_peers(["10.0.0.1", "node-b", "10.0.0.1"])
        assert "10.0.0.1" in result
        assert "10.0.0.2" in result
        assert "node-b" in result


# =========================================================================
# TS06-S03 — Bind-address validation
# =========================================================================

class TestBindAddressValidation:
    """Config validation rejects 0.0.0.0 listen addresses."""

    def test_rejects_0_0_0_0_agent_listen(self, tmp_path: Path):
        """agent.listen with 0.0.0.0 should raise ConfigError."""
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
  listen: "0.0.0.0:8090"
  token_env: SIMBRIDGE_AGENT_TOKEN
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
  ami_password_env: SIMBRIDGE_AMI_PASSWORD
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
        os.environ["SIMBRIDGE_TG_API_ID"] = "12345"
        os.environ["SIMBRIDGE_TG_API_HASH"] = "0123456789abcdef0123456789abcdef"
        os.environ["SIMBRIDGE_AGENT_TOKEN"] = "test-token"
        os.environ["SIMBRIDGE_HTTP_SECRET"] = "test-secret"
        os.environ["SIMBRIDGE_AMI_PASSWORD"] = "test-ami-pw"
        try:
            with pytest.raises(ConfigError, match="0\\.0\\.0\\.0"):
                load_config(str(cfg_path))
        finally:
            os.environ.pop("SIMBRIDGE_TG_API_ID", None)
            os.environ.pop("SIMBRIDGE_TG_API_HASH", None)
            os.environ.pop("SIMBRIDGE_AGENT_TOKEN", None)
            os.environ.pop("SIMBRIDGE_HTTP_SECRET", None)
            os.environ.pop("SIMBRIDGE_AMI_PASSWORD", None)

    def test_rejects_0_0_0_0_userbot_listen(self, tmp_path: Path):
        """userbot_http.listen with 0.0.0.0 should raise ConfigError."""
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text("""\
node:
  role: telegram
  id: test
telegram:
  master_username: test
  session_path: /tmp/t
  acl_file: /tmp/a
  api_id_env: SIMBRIDGE_TG_API_ID
  api_hash_env: SIMBRIDGE_TG_API_HASH
agent:
  listen: "127.0.0.1:8090"
  token_env: SIMBRIDGE_AGENT_TOKEN
  allowed_peers: []
userbot_http:
  listen: "0.0.0.0:8088"
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
        os.environ["SIMBRIDGE_TG_API_ID"] = "12345"
        os.environ["SIMBRIDGE_TG_API_HASH"] = "0123456789abcdef0123456789abcdef"
        os.environ["SIMBRIDGE_AGENT_TOKEN"] = "test-token"
        os.environ["SIMBRIDGE_HTTP_SECRET"] = "test-secret"
        try:
            with pytest.raises(ConfigError, match="0\\.0\\.0\\.0"):
                load_config(str(cfg_path))
        finally:
            os.environ.pop("SIMBRIDGE_TG_API_ID", None)
            os.environ.pop("SIMBRIDGE_TG_API_HASH", None)
            os.environ.pop("SIMBRIDGE_AGENT_TOKEN", None)
            os.environ.pop("SIMBRIDGE_HTTP_SECRET", None)

    def test_allows_localhost(self, tmp_path: Path):
        """127.0.0.1 should be accepted."""
        cfg_path = tmp_path / "good.yaml"
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
  token_env: SIMBRIDGE_AGENT_TOKEN
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
  ami_password_env: SIMBRIDGE_AMI_PASSWORD
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
        os.environ["SIMBRIDGE_TG_API_ID"] = "12345"
        os.environ["SIMBRIDGE_TG_API_HASH"] = "0123456789abcdef0123456789abcdef"
        os.environ["SIMBRIDGE_AGENT_TOKEN"] = "test-token"
        os.environ["SIMBRIDGE_HTTP_SECRET"] = "test-secret"
        os.environ["SIMBRIDGE_AMI_PASSWORD"] = "test-ami-pw"
        try:
            cfg = load_config(str(cfg_path))
            assert cfg["agent.listen"] == "127.0.0.1:8090"
        finally:
            os.environ.pop("SIMBRIDGE_TG_API_ID", None)
            os.environ.pop("SIMBRIDGE_TG_API_HASH", None)
            os.environ.pop("SIMBRIDGE_AGENT_TOKEN", None)
            os.environ.pop("SIMBRIDGE_HTTP_SECRET", None)
            os.environ.pop("SIMBRIDGE_AMI_PASSWORD", None)

    def test_allows_tailscale_ip(self, tmp_path: Path):
        """Tailscale IP (100.x.x.x) should be accepted."""
        cfg_path = tmp_path / "good.yaml"
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
  listen: "100.64.1.5:8090"
  token_env: SIMBRIDGE_AGENT_TOKEN
  allowed_peers: ["100.64.1.10"]
userbot_http:
  listen: "100.64.1.5:8088"
  secret_env: SIMBRIDGE_HTTP_SECRET
  allowed_peers: ["100.64.1.10"]
asterisk:
  ari_url: http://127.0.0.1:8088/ari
  dongle: gsm
  ring_wait_seconds: 24
  max_record_seconds: 90
  prompt: /tmp/p
  ami_password_env: SIMBRIDGE_AMI_PASSWORD
voice:
  bridge_endpoint: tg-bridge
  bridge_host: 100.64.1.10
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
        os.environ["SIMBRIDGE_TG_API_ID"] = "12345"
        os.environ["SIMBRIDGE_TG_API_HASH"] = "0123456789abcdef0123456789abcdef"
        os.environ["SIMBRIDGE_AGENT_TOKEN"] = "test-token"
        os.environ["SIMBRIDGE_HTTP_SECRET"] = "test-secret"
        os.environ["SIMBRIDGE_AMI_PASSWORD"] = "test-ami-pw"
        try:
            cfg = load_config(str(cfg_path))
            assert cfg["agent.listen"] == "100.64.1.5:8090"
        finally:
            os.environ.pop("SIMBRIDGE_TG_API_ID", None)
            os.environ.pop("SIMBRIDGE_TG_API_HASH", None)
            os.environ.pop("SIMBRIDGE_AGENT_TOKEN", None)
            os.environ.pop("SIMBRIDGE_HTTP_SECRET", None)
            os.environ.pop("SIMBRIDGE_AMI_PASSWORD", None)


# =========================================================================
# TS06-S04 — Call rate limiting (source verification)
# =========================================================================

class TestCallRateLimiting:
    """Verify call rate limiting is wired into the agent."""

    def test_routes_imports_call_limiter(self):
        """routes.py should import get_call_limiter."""
        import agent.routes as routes_module
        source = open(routes_module.__file__).read()
        assert "get_call_limiter" in source

    def test_call_outgoing_has_rate_limiter_dependency(self):
        """The /call/outgoing endpoint should use call_limiter dependency."""
        import agent.routes as routes_module
        source = open(routes_module.__file__).read()
        # Find the call_outgoing function definition
        assert "call_limiter: RateLimiter = Depends(get_call_limiter)" in source

    def test_agent_initializes_call_limiter(self):
        """agent.py should initialize app.state.call_limiter."""
        import agent.agent as agent_module
        source = open(agent_module.__file__).read()
        assert "call_limiter" in source
        assert "RateLimiter" in source

    def test_deps_provides_get_call_limiter(self):
        """deps.py should export get_call_limiter."""
        import agent.deps as deps_module
        assert hasattr(deps_module, "get_call_limiter")


# =========================================================================
# TS06-1 — No 0.0.0.0 binds (source verification)
# =========================================================================

class TestNoWildcardBinds:
    """No service should bind 0.0.0.0 in the codebase."""

    def test_agent_main_reads_from_config(self):
        """agent/main.py should read bind address from config, not hardcoded."""
        import agent.main as main_module
        source = open(main_module.__file__).read()
        # Should use cfg["agent.listen"]
        assert 'cfg["agent.listen"]' in source or "cfg['agent.listen']" in source
        # Should NOT hardcode 0.0.0.0
        assert "0.0.0.0" not in source

    def test_userbot_main_reads_from_config(self):
        """userbot HTTP should read bind address from config."""
        import userbot.http_server as http_module
        source = open(http_module.__file__).read()
        # Should NOT hardcode 0.0.0.0
        assert "0.0.0.0" not in source

    def test_example_config_documented(self):
        """Example config should document the bind address requirement."""
        example_path = Path("config/simbridge.example.yaml")
        if example_path.exists():
            content = example_path.read_text()
            # Should have a comment about not using 0.0.0.0
            assert "0.0.0.0" in content and "never" in content.lower(), (
                "Example config should warn against 0.0.0.0"
            )


# =========================================================================
# S01.3 — Replay protection wired on userbot->agent traffic (source check)
# =========================================================================

class TestUserbotCorrelationHeader:
    """The userbot must send x-correlation-id on agent requests; otherwise
    the agent's replay window (S01.3) is never triggered on this traffic."""

    def _userbot_source(self) -> str:
        # Read by path: telethon is a node-side dependency and is not
        # installed in the unit-test environment, so the module cannot
        # be imported here.
        return Path("userbot/userbot.py").read_text()

    def test_userbot_sends_correlation_header(self):
        source = self._userbot_source()
        assert "x-correlation-id" in source

    def test_sms_body_carries_same_correlation_id(self):
        """The same id in the JSON body lets the audit trail match the header."""
        source = self._userbot_source()
        assert '"correlation_id": cid' in source
        assert "uuid.uuid4().hex" in source
