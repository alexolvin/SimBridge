"""Test fixtures shared across the test suite."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_config(tmp_path: Path):
    """Create a minimal valid simbridge.yaml in a temp directory.

    Returns the path to the config file.
    """
    cfg_content = """\
node:
  role: all-in-one
  id: test-node

telegram:
  master_username: testuser
  session_path: /tmp/simbridge_test_session
  acl_file: /tmp/simbridge_test_acl.conf
  api_id_env: SIMBRIDGE_TG_API_ID
  api_hash_env: SIMBRIDGE_TG_API_HASH

agent:
  listen: "127.0.0.1:8090"
  token_env: SIMBRIDGE_AGENT_TOKEN
  userbot_url: http://127.0.0.1:8088
  allowed_peers:
    - "127.0.0.1"

userbot_http:
  listen: "127.0.0.1:8088"
  secret_env: SIMBRIDGE_HTTP_SECRET
  allowed_peers:
    - "127.0.0.1"

asterisk:
  ari_url: http://127.0.0.1:8088/ari
  dongle: gsm
  ring_wait_seconds: 24
  max_record_seconds: 90
  prompt: /var/lib/asterisk/sounds/custom/vm-prompt.ulaw
  ami_host: 127.0.0.1
  ami_port: 5038
  ami_username: simbridge
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
  blacklist: /tmp/simbridge_test_blacklist.txt
  contacts_cache: /tmp/simbridge_test_contacts.csv
  audit_log: /tmp/simbridge_test_audit.jsonl
  sms_correlation: /tmp/simbridge_test_sms_correlation.jsonl
"""
    cfg_path = tmp_path / "simbridge.yaml"
    cfg_path.write_text(cfg_content)

    # Create supporting files
    (tmp_path / "acl.conf").write_text("# test acl\n")
    (tmp_path / "blacklist.txt").write_text("# test blacklist\n")

    # Set env vars for secrets
    os.environ["SIMBRIDGE_TG_API_ID"] = "12345"
    os.environ["SIMBRIDGE_TG_API_HASH"] = "0123456789abcdef0123456789abcdef"
    os.environ["SIMBRIDGE_AGENT_TOKEN"] = "test-token-1234"
    os.environ["SIMBRIDGE_HTTP_SECRET"] = "test-secret-5678"
    os.environ["SIMBRIDGE_AMI_PASSWORD"] = "test-ami-pass"

    yield str(cfg_path)

    # Cleanup env
    for key in [
        "SIMBRIDGE_TG_API_ID",
        "SIMBRIDGE_TG_API_HASH",
        "SIMBRIDGE_AGENT_TOKEN",
        "SIMBRIDGE_HTTP_SECRET",
        "SIMBRIDGE_AMI_PASSWORD",
    ]:
        os.environ.pop(key, None)
