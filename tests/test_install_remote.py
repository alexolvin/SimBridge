"""PC-side orchestrator tests (task #9).

deploy/install_remote.py is imported as a module — safe: module-level
code only defines constants and dataclasses; main() is guarded.

The transport layer (run_remote / upload_remote / check_url / q) is
monkeypatched with fakes, so the full orchestration — preflight,
bootstrap, answers generation, install, result fetch, cross-check,
report — runs in-process without any network.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))

import install_remote as ir  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

def make_q(answers: dict):
    """Fake PC-side q(): substring match on the label."""
    def q(label: str, default: str = "", required: bool = False) -> str:
        for k, v in answers.items():
            if k in label:
                return v
        return default
    return q


class FakeRemote:
    def __init__(self, ips=None, existing=0, install_rc=0,
                 result=None, unreachable=False):
        self.ips = ips or {}
        self.existing = existing
        self.install_rc = install_rc
        self.unreachable = unreachable
        self.result = result if result is not None else {
            "ok": True, "node_id": "x", "role": "gsm",
            "install_type": "single", "version": "0.9.0",
            "agent_token": "t", "http_secret": "h", "bridge_secret": "b",
            "generated_secrets": [],
            "verify": {"passed": ["agent health"], "failed": []},
        }
        self.calls = []
        self.uploads = []          # (node_name, remote_path, data_bytes)

    def run(self, node, transport, cmd, timeout=None, stream=False,
            stdin=None):
        self.calls.append((node.name, cmd))
        if self.unreachable and "echo USER=" in cmd:
            return 255, "Connection refused\n"
        if "echo USER=" in cmd:
            ip = self.ips.get(node.name, "")
            return 0, (f"USER=op\nTS_IP={ip}\nEXISTING={self.existing}\n"
                       f"SUDO_OK=1\nTS_PRESENT=1\n")
        if "mkdir -p" in cmd:
            return 0, ""
        if "py_compile" in cmd:
            return 0, "BOOTSTRAP_OK\n"
        if "--answers" in cmd:
            return self.install_rc, "installer log line\n"
        if "result.json" in cmd and "base64" in cmd:
            if self.install_rc == 1:
                return 1, ""
            return 0, base64.b64encode(
                json.dumps(self.result).encode()).decode() + "\n"
        if "rm -f" in cmd:
            return 0, ""
        if "curl -sf" in cmd:
            return 0, "{}\n"
        return 0, ""

    def upload(self, node, transport, data, remote_path, mode="0600",
               timeout=120):
        self.uploads.append((node.name, remote_path, data))
        self.calls.append((node.name, f"UPLOAD {remote_path}"))
        return 0, "UPLOAD_OK\n"


@pytest.fixture()
def fake_remote(monkeypatch):
    fr = FakeRemote()
    monkeypatch.setattr(ir, "run_remote", fr.run)
    monkeypatch.setattr(ir, "upload_remote", fr.upload)
    monkeypatch.setattr(ir, "detect_transport", lambda: "tailscale")
    monkeypatch.setattr(ir, "pc_tailscale_ips", lambda: {})
    return fr


SINGLE_Q = {
    "Deployment:": "s",
    "Node 1 tailscale name": "node-a",
    "Node 1 ssh user": "op",
    "Node id": "sim-1",
    "SIM phone": "+7926XXXXXXX",
    "Modem model": "Quectel EC20",
    "Telegram API_ID": "123456",
    "Telegram API_HASH": "0" * 32,
    "Master Telegram": "master",
    "ACL": "123456789",
}

TWO_Q = {
    "Deployment:": "t",
    "Node 1 tailscale name": "node-a",
    "Node 1 ssh user": "op",
    "Node 2 tailscale name": "node-b",
    "Node 2 ssh user": "op",
    "Which node is the GSM": "1",
    "Node node-a id": "gsm-1",
    "Node node-b id": "tg-1",
    "SIM phone": "+7926XXXXXXX",
    "Modem model": "Quectel EC20",
    "Telegram API_ID": "123456",
    "Telegram API_HASH": "0" * 32,
    "Master Telegram": "master",
    "ACL": "123456789",
}


def run_orchestrate(monkeypatch, q_answers, dry_run=False):
    monkeypatch.setattr(ir, "q", make_q(q_answers))
    args = argparse.Namespace(dry_run=dry_run)
    return ir.orchestrate(args)


def uploaded_answers(fr: FakeRemote, node_name: str) -> str:
    for name, path, data in fr.uploads:
        if name == node_name and path == ir.ANSWERS_PATH:
            return data.decode()
    raise AssertionError(f"no answers upload for {node_name}")


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

class TestAnswersEnvText:
    def test_plain_value_unquoted(self):
        t = ir.answers_env_text({"node_id": "gsm-1"})
        assert "node_id = gsm-1" in t

    def test_space_value_quoted(self):
        t = ir.answers_env_text(
            {"node_role": ir.OPT_ROLE_GSM})
        assert 'node_role = "GSM node (Asterisk + modem)"' in t

    def test_roundtrip_through_install_parser(self):
        d = {"node_id": "gsm-1", "node_role": ir.OPT_ROLE_GSM,
             "peer_ip": "100.64.0.3", "ami_password": ""}
        t = ir.answers_env_text(d)
        # Parse with the REAL install.py parser (deploy/ on sys.path).
        import os
        import tempfile
        import install  # noqa: E402
        with tempfile.NamedTemporaryFile("w", suffix=".env",
                                         delete=False) as f:
            f.write(t)
            p = f.name
        try:
            got = install._load_answers(p)
        finally:
            os.unlink(p)
        assert got["node_id"] == "gsm-1"
        assert got["node_role"] == "GSM node (Asterisk + modem)"
        assert got["peer_ip"] == "100.64.0.3"
        assert got["ami_password"] == ""


class TestBuildAnswers:
    def test_single_node(self):
        n = ir.Node(name="a", ssh_user="op", role="all-in-one",
                    node_id="sim-1", ts_present=True)
        sh = ir.Shared(agent_token="t" * 32, bridge_secret="b" * 32,
                       http_secret="h" * 32, acl_ids="1",
                       sim_phone="+7926XXXXXXX", modem_model="M",
                       tg_api_id="1", tg_api_hash="0" * 32,
                       tg_username="master")
        d = ir.build_answers(n, sh)
        assert d["install_type"] == ir.OPT_SINGLE
        assert "node_role" not in d          # single: role is implicit
        assert "own_ip" not in d and "peer_ip" not in d
        assert d["tg_login"] == "false"
        assert d["peer_ready"] == "false"
        assert d["install_tailscale"] == "false"   # already present
        assert d["agent_token"] == "t" * 32
        assert d["modem_model"] == "M"
        assert d["tg_api_id"] == "1"

    def test_distributed_roles_and_ips(self):
        gsm = ir.Node(name="g", ssh_user="op", role="gsm",
                      node_id="gsm-1", own_ip="100.64.0.2",
                      peer_ip="100.64.0.3")
        tg = ir.Node(name="t", ssh_user="op", role="telegram",
                     node_id="tg-1", own_ip="100.64.0.3",
                     peer_ip="100.64.0.2")
        sh = ir.Shared(install_type="distributed",
                       agent_token="tok", bridge_secret="bs",
                       http_secret="hs", acl_ids="1",
                       tg_api_id="1", tg_api_hash="0" * 32,
                       tg_username="master")
        dg, dt = ir.build_answers(gsm, sh), ir.build_answers(tg, sh)
        assert dg["node_role"] == ir.OPT_ROLE_GSM
        assert dt["node_role"] == ir.OPT_ROLE_TG
        assert dg["own_ip"] == "100.64.0.2" and dg["peer_ip"] == "100.64.0.3"
        assert dt["own_ip"] == "100.64.0.3" and dt["peer_ip"] == "100.64.0.2"
        # The pre-generated secrets are identical on both nodes.
        for k in ("agent_token", "bridge_secret", "http_secret"):
            assert dg[k] == dt[k]
        # GSM node carries no Telegram credentials (installer never asks).
        assert "tg_api_id" not in dg
        assert dt["tg_api_id"] == "1"

    def test_empty_optional_values_omitted(self):
        # Update mode: credential fields left blank on the PC must be
        # ABSENT from the answers file — install.py _ans_str then falls
        # back to the node's existing config (missing key -> default).
        gsm = ir.Node(name="g", ssh_user="op", role="gsm", node_id="gsm-1",
                      own_ip="100.64.0.2", peer_ip="100.64.0.3")
        tg = ir.Node(name="t", ssh_user="op", role="telegram", node_id="tg-1",
                     own_ip="100.64.0.3", peer_ip="100.64.0.2")
        sh = ir.Shared(install_type="distributed", dongle_name="",
                       agent_token="tok", bridge_secret="bs",
                       http_secret="hs", acl_ids="1")
        dg, dt = ir.build_answers(gsm, sh), ir.build_answers(tg, sh)
        # Optional fields empty -> omitted on the node that would use them.
        for k in ("modem_model", "sim_phone", "dongle_name"):
            assert k not in dg
        for k in ("tg_api_id", "tg_api_hash", "tg_username"):
            assert k not in dt
        # ...and role-foreign fields stay absent as before.
        for k in ("tg_api_id", "tg_api_hash", "tg_username"):
            assert k not in dg
        for k in ("modem_model", "sim_phone", "dongle_name"):
            assert k not in dt
        # acl_ids is ALWAYS sent: install.py marks it required=True and
        # never falls back to the existing ACL value.
        assert dg["acl_ids"] == "1" and dt["acl_ids"] == "1"
        # Shared secrets + network wiring still present on both nodes.
        for k in ("agent_token", "bridge_secret", "http_secret",
                  "own_ip", "peer_ip"):
            assert dg[k] and dt[k]
        assert dg["own_ip"] == "100.64.0.2" and dt["own_ip"] == "100.64.0.3"
        assert dg["peer_ip"] == "100.64.0.3" and dt["peer_ip"] == "100.64.0.2"

    def test_action_keys(self):
        n = ir.Node(name="a", ssh_user="op", role="gsm", node_id="g1",
                    ts_present=True)
        sh = ir.Shared(install_type="distributed", acl_ids="1",
                       agent_token="t", bridge_secret="b", http_secret="h")
        n.action = "wipe"
        assert ir.build_answers(n, sh)["action"] == ir.OPT_WIPE
        n.action = "update"
        assert ir.build_answers(n, sh)["action"] == ir.OPT_UPDATE
        n.action = ""
        assert "action" not in ir.build_answers(n, sh)


class TestParsePreflight:
    def test_parse(self):
        d = ir.parse_preflight(
            "USER=op\nTS_IP=100.64.0.2\nEXISTING=1\nSUDO_OK=1\n"
            "TS_PRESENT=1\n")
        assert d == {"USER": "op", "TS_IP": "100.64.0.2", "EXISTING": "1",
                     "SUDO_OK": "1", "TS_PRESENT": "1"}

    def test_empty_ip(self):
        d = ir.parse_preflight("USER=op\nTS_IP=\nSUDO_OK=0\nTS_PRESENT=0\n")
        assert d["TS_IP"] == "" and d["SUDO_OK"] == "0"


# ---------------------------------------------------------------------------
# Orchestration (fake transport)
# ---------------------------------------------------------------------------

class TestOrchestrateSingle:
    def test_full_success(self, fake_remote, monkeypatch, capsys):
        rc = run_orchestrate(monkeypatch, SINGLE_Q)
        assert rc == 0
        out = capsys.readouterr().out
        assert "DEPLOYMENT REPORT" in out
        assert "OK" in out
        # install.py + answers both uploaded
        uploaded = {(n, p) for (n, p, _) in fake_remote.uploads}
        assert ("node-a", ir.INSTALLER_PATH) in uploaded
        assert ("node-a", ir.ANSWERS_PATH) in uploaded
        # single-node cross-check uses on-node curl to loopback
        curls = [c for (_, c) in fake_remote.calls if "curl -sf" in c]
        assert any(f":{ir.USERBOT_PORT}/health" in c for c in curls)
        assert any(f":{ir.AGENT_PORT}/v1/health" in c for c in curls)
        # answers content
        a = uploaded_answers(fake_remote, "node-a")
        assert "node_id = sim-1" in a            # no space → unquoted
        assert "tg_login = false" in a
        assert "peer_ready = false" in a
        assert "install_tailscale = false" in a
        assert "agent_token = " in a

    def test_dry_run_touches_nothing(self, fake_remote, monkeypatch,
                                     capsys):
        rc = run_orchestrate(monkeypatch, SINGLE_Q, dry_run=True)
        assert rc == 0
        assert fake_remote.calls == [] and fake_remote.uploads == []
        out = capsys.readouterr().out
        assert "DRY RUN" in out and "node-a" in out


class TestOrchestrateTwoNode:
    def test_full_success(self, fake_remote, monkeypatch, capsys):
        fake_remote.ips = {"node-a": "100.64.0.2",
                           "node-b": "100.64.0.3"}
        checked = []
        monkeypatch.setattr(
            ir, "check_url",
            lambda url, token=None: (checked.append(url) or
                                     (True, "{}")))
        rc = run_orchestrate(monkeypatch, TWO_Q)
        assert rc == 0
        # Cross-check from the PC hits the right endpoints.
        assert f"http://100.64.0.3:{ir.USERBOT_PORT}/health" in checked
        assert f"http://100.64.0.2:{ir.AGENT_PORT}/v1/health" in checked
        ag, at = uploaded_answers(fake_remote, "node-a"), \
            uploaded_answers(fake_remote, "node-b")
        assert "node_role = \"GSM node (Asterisk + modem)\"" in ag
        assert "node_role = \"Telegram node (userbot)\"" in at
        assert "own_ip = 100.64.0.2" in ag and "peer_ip = 100.64.0.3" in ag
        assert "own_ip = 100.64.0.3" in at and "peer_ip = 100.64.0.2" in at
        # One token pair for the whole deployment.
        gt = [l for l in ag.splitlines() if l.startswith("agent_token")]
        tt = [l for l in at.splitlines() if l.startswith("agent_token")]
        assert gt == tt and gt[0]
        # TG node must carry the Telegram credentials, GSM node must not.
        assert "tg_api_id" in at and "tg_api_id" not in ag


class TestOrchestrateFailures:
    def test_install_exit_1(self, fake_remote, monkeypatch, capsys):
        fake_remote.install_rc = 1
        rc = run_orchestrate(monkeypatch, SINGLE_Q)
        assert rc == 2
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "exit 1" in out

    def test_install_exit_2_verify_issues(self, fake_remote, monkeypatch,
                                          capsys):
        fake_remote.install_rc = 2
        fake_remote.result = {
            "ok": False, "node_id": "sim-1", "role": "all-in-one",
            "install_type": "single", "version": "0.9.0",
            "agent_token": "t", "http_secret": "h", "bridge_secret": "b",
            "generated_secrets": [],
            "verify": {"passed": ["agent health"],
                       "failed": ["sweep timer"]},
        }
        rc = run_orchestrate(monkeypatch, SINGLE_Q)
        assert rc == 2
        out = capsys.readouterr().out
        assert "installed, verify issues" in out
        assert "sweep timer" in out

    def test_cross_check_failure(self, fake_remote, monkeypatch, capsys):
        monkeypatch.setattr(ir, "check_url",
                            lambda url, token=None: (False, "refused"))
        fake_remote.ips = {"node-a": "100.64.0.2",
                           "node-b": "100.64.0.3"}
        rc = run_orchestrate(monkeypatch, TWO_Q)
        assert rc == 2
        out = capsys.readouterr().out
        assert "cross-check" in out

    def test_unreachable_node_blocks_it(self, fake_remote, monkeypatch,
                                        capsys):
        fake_remote.unreachable = True
        rc = run_orchestrate(monkeypatch, SINGLE_Q)
        assert rc == 2
        out = capsys.readouterr().out
        assert "unreachable" in out
        # No install attempted on the dead node.
        assert not any("--answers" in c for (_, c) in fake_remote.calls)


class TestSecretsFile:
    def test_load_ok(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"agent_token": "a", "bridge_secret": "b",
                                 "http_secret": "c"}))
        d = ir.load_shared_secrets(str(p))
        assert d == {"agent_token": "a", "bridge_secret": "b",
                     "http_secret": "c"}

    def test_missing_key_exits(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"agent_token": "a"}))
        with pytest.raises(SystemExit) as e:
            ir.load_shared_secrets(str(p))
        assert e.value.code == 1

    def test_empty_value_exits(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"agent_token": "", "bridge_secret": "b",
                                 "http_secret": "c"}))
        with pytest.raises(SystemExit) as e:
            ir.load_shared_secrets(str(p))
        assert e.value.code == 1

    def test_not_a_dict_exits(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text("[1, 2]")
        with pytest.raises(SystemExit) as e:
            ir.load_shared_secrets(str(p))
        assert e.value.code == 1

    def test_unreadable_exits(self, tmp_path):
        with pytest.raises(SystemExit) as e:
            ir.load_shared_secrets(str(tmp_path / "nope.json"))
        assert e.value.code == 1

    def test_orchestrator_reuses_file(self, fake_remote, monkeypatch,
                                      tmp_path, capsys):
        p = tmp_path / "s.json"
        pair = {"agent_token": "reuse" + "a" * 27,
                "bridge_secret": "reuse" + "b" * 27,
                "http_secret": "reuse" + "c" * 27}
        p.write_text(json.dumps(pair))
        monkeypatch.setattr(ir, "q", make_q(SINGLE_Q))
        args = argparse.Namespace(dry_run=True, secrets_file=str(p))
        rc = ir.orchestrate(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert f"agent_token = {pair['agent_token']}" in out
        assert f"bridge_secret = {pair['bridge_secret']}" in out
        assert f"http_secret = {pair['http_secret']}" in out


class TestExistingInstall:
    def test_wipe_action(self, fake_remote, monkeypatch):
        fake_remote.existing = 1
        q = dict(SINGLE_Q, **{"existing installation": "w"})
        rc = run_orchestrate(monkeypatch, q)
        assert rc == 0
        a = uploaded_answers(fake_remote, "node-a")
        assert f"action = \"{ir.OPT_WIPE}\"" in a

    def test_update_action_default(self, fake_remote, monkeypatch):
        fake_remote.existing = 1
        rc = run_orchestrate(monkeypatch, SINGLE_Q)   # no "wipe" answer
        assert rc == 0
        a = uploaded_answers(fake_remote, "node-a")
        assert f"action = \"{ir.OPT_UPDATE}\"" in a
