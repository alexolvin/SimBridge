"""Non-interactive installer mode tests (task #8).

deploy/install.py is imported as a module — this is safe: the
module-level code only defines constants, the answer machinery, and the
shared ``s = S()`` state object; main() is guarded by __main__.

Covered:
- _load_answers (parsing, quotes, comments, malformed, unknown keys)
- _ans_str / _ans_bool semantics
- ask / ask_yn / pick in non-interactive mode
- phase_type / phase_gather (GSM + Telegram roles)
- _validate_noninteractive (Telegram node parity)
- _write_result (JSON contract + 0600)
- _load_existing_config node.role parsing (for --tg-login)
"""

from __future__ import annotations

import json
import stat
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))

import install  # noqa: E402

# Module-global installer state fields the tests touch. The fixture
# snapshots and restores them — install.s is a shared singleton.
_STATE_FIELDS = (
    "install_type", "node_role", "action", "src_version",
    "modem_model", "sim_phone", "dongle_name", "ami_pw",
    "tg_api_id", "tg_api_hash", "tg_username",
    "agent_token", "http_secret", "bridge_secret",
    "own_ip", "peer_ip", "acl_ids",
    "do_ts", "do_ts_opt", "usb_modems", "ts_ip", "has_ts",
    "node_id", "generated_secrets", "verify_issues",
)
_LIST_FIELDS = ("usb_modems", "generated_secrets", "verify_issues")


@pytest.fixture()
def ni_state():
    """Non-interactive mode ON + pristine installer state per test."""
    saved = {f: getattr(install.s, f) for f in _STATE_FIELDS}
    for f in _STATE_FIELDS:
        if f in _LIST_FIELDS:
            setattr(install.s, f, [])
        elif isinstance(saved[f], str):
            setattr(install.s, f, "")
        else:
            setattr(install.s, f, saved[f])
    old_ni, old_answers = install._NONINTERACTIVE, install._ANSWERS
    install._NONINTERACTIVE = True
    install._ANSWERS = {}
    yield
    for f, v in saved.items():
        setattr(install.s, f, v)
    install._NONINTERACTIVE, install._ANSWERS = old_ni, old_answers


# ---------------------------------------------------------------------------
# _load_answers
# ---------------------------------------------------------------------------

class TestLoadAnswers:
    def test_parse_basic(self, tmp_path):
        p = tmp_path / "a.env"
        p.write_text('node_id = gsm-1\nnode_role = "GSM node (Asterisk + modem)"\n')
        a = install._load_answers(str(p))
        assert a["node_id"] == "gsm-1"
        assert a["node_role"] == "GSM node (Asterisk + modem)"

    def test_comments_blanks_single_quotes(self, tmp_path):
        p = tmp_path / "a.env"
        p.write_text("# comment\n\npeer_ip = '100.64.0.3'\n")
        a = install._load_answers(str(p))
        assert a == {"peer_ip": "100.64.0.3"}

    def test_malformed_line_skipped(self, tmp_path, capsys):
        p = tmp_path / "a.env"
        p.write_text("node_id = x\nthis line has no equals sign\n")
        a = install._load_answers(str(p))
        assert a == {"node_id": "x"}
        assert "malformed" in capsys.readouterr().err

    def test_unknown_key_warned_not_fatal(self, tmp_path, capsys):
        p = tmp_path / "a.env"
        p.write_text("node_id = x\ntotally_unknown_key = 1\n")
        a = install._load_answers(str(p))
        assert a == {"node_id": "x"}
        assert "Unknown key" in capsys.readouterr().err

    def test_missing_file_exits_1(self, tmp_path):
        with pytest.raises(SystemExit) as e:
            install._load_answers(str(tmp_path / "nope.env"))
        assert e.value.code == 1


# ---------------------------------------------------------------------------
# _ans_str / _ans_bool
# ---------------------------------------------------------------------------

class TestAnsStr:
    def test_present(self, ni_state):
        install._ANSWERS = {"node_id": "gsm-1"}
        assert install._ans_str("node_id", "d") == "gsm-1"

    def test_missing_optional_gives_default(self, ni_state):
        assert install._ans_str("sim_phone", "+7926XXXXXXX") == "+7926XXXXXXX"

    def test_missing_required_exits(self, ni_state):
        with pytest.raises(SystemExit) as e:
            install._ans_str("acl_ids", "", required=True)
        assert e.value.code == 1

    def test_empty_required_exits(self, ni_state):
        install._ANSWERS = {"acl_ids": ""}
        with pytest.raises(SystemExit):
            install._ans_str("acl_ids", "", required=True)

    def test_empty_optional_gives_default(self, ni_state):
        install._ANSWERS = {"ami_password": ""}
        assert install._ans_str("ami_password", "kept") == "kept"


class TestAnsBool:
    @pytest.mark.parametrize("v,expected", [
        ("y", True), ("yes", True), ("true", True), ("1", True),
        ("n", False), ("no", False), ("false", False), ("0", False),
    ])
    def test_values(self, ni_state, v, expected):
        install._ANSWERS = {"peer_ready": v}
        assert install._ans_bool("peer_ready", not expected) is expected

    def test_missing_gives_default(self, ni_state):
        assert install._ans_bool("peer_ready", False) is False

    def test_invalid_exits(self, ni_state):
        install._ANSWERS = {"peer_ready": "maybe"}
        with pytest.raises(SystemExit):
            install._ans_bool("peer_ready", True)


# ---------------------------------------------------------------------------
# ask / ask_yn / pick in non-interactive mode
# ---------------------------------------------------------------------------

class TestPromptHelpers:
    def test_ask_value(self, ni_state):
        install._ANSWERS = {"node_id": "n1"}
        assert install.ask("Node ID", "d", key="node_id") == "n1"

    def test_ask_missing_gives_default(self, ni_state):
        assert install.ask("Node ID", "d", key="node_id") == "d"

    def test_ask_yn_value(self, ni_state):
        install._ANSWERS = {"tg_login": "false"}
        assert install.ask_yn("Log in now?", True, key="tg_login") is False

    def test_ask_yn_missing_gives_default(self, ni_state):
        assert install.ask_yn("Log in now?", True, key="tg_login") is True

    def test_pick_exact_option(self, ni_state):
        opts = ["Single-node (all-in-one)", "Two-node (distributed)"]
        install._ANSWERS = {"install_type": "Two-node (distributed)"}
        assert install.pick("t:", opts, 0, key="install_type") \
            == "Two-node (distributed)"

    def test_pick_by_index(self, ni_state):
        install._ANSWERS = {"action": "1"}
        opts = ["Remove existing and start fresh", "Update in place", "Abort"]
        assert install.pick("t:", opts, 1, key="action") \
            == "Remove existing and start fresh"

    def test_pick_invalid_exits(self, ni_state):
        install._ANSWERS = {"install_type": "bogus"}
        with pytest.raises(SystemExit):
            install.pick("t:", ["a", "b"], 0, key="install_type")

    def test_pick_missing_gives_default_option(self, ni_state):
        assert install.pick("t:", ["a", "b"], 1, key="install_type") == "b"

    def test_pick_empty_value_gives_default_option(self, ni_state):
        install._ANSWERS = {"install_type": ""}
        assert install.pick("t:", ["a", "b"], 0, key="install_type") == "a"


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

class TestPhaseType:
    def test_distributed_gsm(self, ni_state):
        install._ANSWERS = {
            "install_type": "Two-node (distributed)",
            "node_role": "GSM node (Asterisk + modem)",
        }
        install.phase_type()
        assert install.s.install_type == "distributed"
        assert install.s.node_role == "gsm"

    def test_distributed_telegram(self, ni_state):
        install._ANSWERS = {
            "install_type": "Two-node (distributed)",
            "node_role": "Telegram node (userbot)",
        }
        install.phase_type()
        assert install.s.node_role == "telegram"

    def test_single(self, ni_state):
        install._ANSWERS = {"install_type": "Single-node (all-in-one)"}
        install.phase_type()
        assert install.s.install_type == "single"
        assert install.s.node_role == "all-in-one"


class TestPhaseGatherGsm:
    @staticmethod
    def _answers(**over):
        a = {
            "node_id": "gsm-1",
            "modem_model": "Quectel EC20",
            "sim_phone": "+7926XXXXXXX",
            "dongle_name": "gsm",
            "ami_password": "",
            "agent_token": "",      # empty -> auto-generate
            "bridge_secret": "",    # empty -> auto-generate
            "http_secret": "",      # empty -> auto-generate
            "own_ip": "100.64.0.2",
            "peer_ip": "100.64.0.3",
            "acl_ids": "123456789",
        }
        a.update(over)
        return a

    def test_full_gsm_auto_generates_secrets(self, ni_state):
        install.s.install_type = "distributed"
        install.s.node_role = "gsm"
        install._ANSWERS = self._answers()
        install.phase_gather()
        st = install.s
        assert st.node_id == "gsm-1"
        assert st.modem_model == "Quectel EC20"
        assert st.sim_phone == "+7926XXXXXXX"
        assert st.dongle_name == "gsm"
        assert st.own_ip == "100.64.0.2"
        assert st.peer_ip == "100.64.0.3"
        assert st.acl_ids == "123456789"
        # Empty shared secrets are auto-generated on the GSM node and
        # reported for the orchestrator (_rand(32) = 16 bytes = 32 hex).
        assert len(st.agent_token) == 32
        assert len(st.bridge_secret) == 32
        assert len(st.http_secret) == 32
        assert st.generated_secrets == [
            "agent_token", "bridge_secret", "http_secret"]

    def test_gsm_keeps_provided_secrets(self, ni_state):
        install.s.install_type = "distributed"
        install.s.node_role = "gsm"
        install._ANSWERS = self._answers(
            agent_token="tok" + "0" * 31,
            bridge_secret="bs" + "0" * 30,
            http_secret="hs" + "0" * 30,
        )
        install.phase_gather()
        assert install.s.agent_token == "tok" + "0" * 31
        assert install.s.bridge_secret == "bs" + "0" * 30
        assert install.s.http_secret == "hs" + "0" * 30
        assert install.s.generated_secrets == []

    def test_missing_acl_ids_exits(self, ni_state):
        install.s.install_type = "distributed"
        install.s.node_role = "gsm"
        a = self._answers()
        del a["acl_ids"]
        install._ANSWERS = a
        with pytest.raises(SystemExit) as e:
            install.phase_gather()
        assert e.value.code == 1

    def test_missing_modem_model_fails_validation(self, ni_state):
        install.s.install_type = "distributed"
        install.s.node_role = "gsm"
        install._ANSWERS = self._answers(modem_model="")
        with pytest.raises(SystemExit) as e:
            install.phase_gather()
        assert e.value.code == 1

    def test_missing_own_ip_fails_validation(self, ni_state):
        install.s.install_type = "distributed"
        install.s.node_role = "gsm"
        install._ANSWERS = self._answers(own_ip="")
        with pytest.raises(SystemExit) as e:
            install.phase_gather()
        assert e.value.code == 1


class TestPhaseGatherTelegram:
    @staticmethod
    def _answers(**over):
        a = {
            "node_id": "tg-1",
            "tg_api_id": "123456",
            "tg_api_hash": "0" * 32,
            "tg_username": "master",
            "agent_token": "tok" + "0" * 31,
            "bridge_secret": "bs" + "0" * 30,
            "http_secret": "hs" + "0" * 30,
            "own_ip": "100.64.0.2",
            "peer_ip": "100.64.0.3",
            "acl_ids": "123456789",
        }
        a.update(over)
        return a

    def test_full_telegram_passes(self, ni_state):
        install.s.install_type = "distributed"
        install.s.node_role = "telegram"
        install._ANSWERS = self._answers()
        install.phase_gather()
        assert install.s.tg_api_id == "123456"
        assert install.s.tg_username == "master"
        assert install.s.agent_token == "tok" + "0" * 31
        # A pure Telegram node auto-generates nothing.
        assert install.s.generated_secrets == []

    def test_missing_shared_secrets_fail_validation(self, ni_state):
        install.s.install_type = "distributed"
        install.s.node_role = "telegram"
        install._ANSWERS = self._answers(
            tg_api_id="", agent_token="", bridge_secret="", http_secret="")
        with pytest.raises(SystemExit) as e:
            install.phase_gather()
        assert e.value.code == 1


# ---------------------------------------------------------------------------
# _write_result
# ---------------------------------------------------------------------------

class TestWriteResult:
    def test_result_json_contract(self, ni_state, tmp_path, monkeypatch):
        out = tmp_path / "r.json"
        monkeypatch.setattr(install, "_RESULT_PATH", str(out))
        st = install.s
        st.node_id = "gsm-1"
        st.node_role = "gsm"
        st.install_type = "distributed"
        st.src_version = "0.9.0"
        st.own_ip = "100.64.0.2"
        st.peer_ip = "100.64.0.3"
        st.agent_token = "tok"
        st.http_secret = "hs"
        st.bridge_secret = "bs"
        st.generated_secrets = ["agent_token"]
        st.verify_issues = [
            ("agent health", False, ""),
            ("sweep timer", True, "systemctl start simbridge-sweep.timer"),
            (f"Cross-node to tg (100.64.0.3) — SKIPPED", False, ""),
        ]
        install._write_result()

        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        d = json.loads(out.read_text())
        assert d["ok"] is False
        assert d["node_id"] == "gsm-1"
        assert d["role"] == "gsm"
        assert d["install_type"] == "distributed"
        assert d["version"] == "0.9.0"
        assert d["own_ip"] == "100.64.0.2"
        assert d["peer_ip"] == "100.64.0.3"
        assert d["agent_token"] == "tok"
        assert d["http_secret"] == "hs"
        assert d["bridge_secret"] == "bs"
        assert d["generated_secrets"] == ["agent_token"]
        assert d["verify"]["passed"] == ["agent health"]
        assert d["verify"]["failed"] == ["sweep timer"]
        assert d["verify"]["fixes"]["sweep timer"] \
            == "systemctl start simbridge-sweep.timer"
        assert d["verify"]["skipped"] \
            == [f"Cross-node to tg (100.64.0.3) — SKIPPED"]

    def test_all_passed_is_ok(self, ni_state, tmp_path, monkeypatch):
        out = tmp_path / "r.json"
        monkeypatch.setattr(install, "_RESULT_PATH", str(out))
        install.s.node_role = "gsm"
        install.s.install_type = "single"
        install.s.verify_issues = [("agent health", False, "")]
        install._write_result()
        d = json.loads(out.read_text())
        assert d["ok"] is True
        assert d["verify"]["failed"] == []


# ---------------------------------------------------------------------------
# _load_existing_config — node.role (for --tg-login)
# ---------------------------------------------------------------------------

class TestLoadExistingRole:
    def test_node_role_parsed(self, tmp_path, monkeypatch, ni_state):
        cfg = tmp_path / "simbridge.yaml"
        cfg.write_text("node:\n  id: tg-1\n  role: telegram\n")
        monkeypatch.setattr(install, "CONF_FILE", str(cfg))
        monkeypatch.setattr(install, "ENV_FILE", str(tmp_path / "env"))
        monkeypatch.setattr(install, "ACL_FILE", str(tmp_path / "acl.conf"))
        install._load_existing_config()
        assert install.s.node_role == "telegram"
        assert install.s.node_id == "tg-1"

    def test_missing_config_leaves_role_empty(self, tmp_path, monkeypatch,
                                              ni_state):
        monkeypatch.setattr(install, "CONF_FILE",
                            str(tmp_path / "absent.yaml"))
        install._load_existing_config()
        assert install.s.node_role == ""


# ---------------------------------------------------------------------------
# _chown_asterisk — POSIX chown (Path.chown is Windows-only; a live deploy
# on AlmaLinux crashed with AttributeError before the os.chown fix)
# ---------------------------------------------------------------------------

class TestChownAsterisk:
    def test_chowns_to_asterisk_user(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(install.pwd, "getpwnam",
                            lambda name: types.SimpleNamespace(
                                pw_uid=502, pw_gid=502))
        monkeypatch.setattr(install.os, "chown",
                            lambda *a: calls.append(a))
        target = tmp_path / "manager_custom.conf"
        target.write_text("[simbridge]\n")
        install._chown_asterisk(target)
        assert calls == [(target, 502, 502)]

    def test_missing_asterisk_user_is_swallowed(self, tmp_path,
                                                 monkeypatch):
        def _no_user(name):
            raise KeyError(name)
        monkeypatch.setattr(install.pwd, "getpwnam", _no_user)
        calls = []
        monkeypatch.setattr(install.os, "chown",
                            lambda *a: calls.append(a))
        target = tmp_path / "pjsip.conf"
        target.write_text("")
        install._chown_asterisk(target)   # must not raise
        assert calls == []
