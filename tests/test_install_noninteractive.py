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
- _merge_env KEY=VALUE format (no spaces, legacy self-normalization)
- _render_modules_conf / _write_modules_conf (S06.2 load list: backup,
  hard-fail on missing required module, restart flag)
- phase_start restart-vs-reload policy (modules.conf needs a restart)
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
    "node_id", "generated_secrets", "verify_issues", "ast_config_changed",
    "ast_modules_changed", "ast_core_changed",
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


# ---------------------------------------------------------------------------
# _setup_ami — manager.conf include directive + chown/chmod enforcement
# ---------------------------------------------------------------------------

class TestSetupAmi:
    @pytest.fixture()
    def ami_env(self, tmp_path, monkeypatch, ni_state):
        ast = tmp_path / "asterisk"
        ast.mkdir()
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        install.s.ami_pw = "test-ami-secret-123"
        return ast

    def test_fresh_writes_hash_include(self, ami_env):
        install._setup_ami()
        main_txt = (ami_env / "manager.conf").read_text()
        custom_txt = (ami_env / "manager_custom.conf").read_text()
        # Asterisk's include directive is '#include' — a bare 'Include'
        # line is silently dropped, so the [simbridge] user never loaded.
        assert "#include manager_custom.conf" in main_txt
        assert "Include manager_custom.conf" not in main_txt.replace(
            "#include manager_custom.conf", "")
        assert "secret = test-ami-secret-123" in custom_txt
        assert install.s.ast_config_changed is True

    def test_legacy_include_normalized(self, ami_env):
        (ami_env / "manager.conf").write_text(
            "[general]\nenabled = yes\nport = 5038\n"
            "Include manager_custom.conf\n")
        install._setup_ami()
        txt = (ami_env / "manager.conf").read_text()
        assert txt.count("#include manager_custom.conf") == 1
        assert "Include manager_custom.conf" not in txt.replace(
            "#include manager_custom.conf", "")
        assert "enabled = yes" in txt
        assert install.s.ast_config_changed is True

    def test_hash_include_idempotent(self, ami_env):
        (ami_env / "manager.conf").write_text(
            "[general]\nenabled = yes\nport = 5038\n"
            "#include manager_custom.conf\n")
        install._setup_ami()
        txt = (ami_env / "manager.conf").read_text()
        assert txt.count("#include manager_custom.conf") == 1

    def test_chown_enforced_when_content_matches(self, ami_env, monkeypatch):
        install._setup_ami()          # first run: creates both files
        chowns = []
        monkeypatch.setattr(install, "_chown_asterisk",
                            lambda p: chowns.append(str(p)))
        install.s.ast_config_changed = False
        install._setup_ami()          # second run: content already right
        # Ownership/perm is enforced even when the content matches — a
        # file left root:root by an earlier crashed run still drifts.
        # manager.conf is untouched (no content change) → only the
        # custom file is re-chowned.
        assert chowns == [str(ami_env / "manager_custom.conf")]
        assert install.s.ast_config_changed is False


# ---------------------------------------------------------------------------
# phase_verify — chan_dongle module detection (running, not active)
# ---------------------------------------------------------------------------

class TestDongleModuleOutput:
    # Real `asterisk -rx 'module show like dongle'` output (verified on
    # the GSM node) — note the Status column: "Running", not "active".
    _RUNNING_TABLE = (
        "Module                         Description"
        "                              Use Count  Status      Support Level\n"
        "chan_dongle.so                 Huawei 3G Dongle Channel Driver"
        "          0          Running          extended\n"
        "1 modules loaded"
    )

    def test_returns_immediately_when_module_listed(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(install, "run_q",
                            lambda cmd: types.SimpleNamespace(
                                stdout=self._RUNNING_TABLE, returncode=0))
        monkeypatch.setattr(install.time, "sleep", sleeps.append)
        out = install._dongle_module_output(retries=5, delay=0.0)
        assert "chan_dongle" in out
        assert sleeps == []

    def test_retries_while_asterisk_settles(self, monkeypatch):
        outs = iter(["0 modules loaded", self._RUNNING_TABLE])
        monkeypatch.setattr(install, "run_q",
                            lambda cmd: types.SimpleNamespace(
                                stdout=next(outs), returncode=0))
        monkeypatch.setattr(install.time, "sleep", lambda _d: None)
        out = install._dongle_module_output(retries=3, delay=0.0)
        assert "chan_dongle" in out

    def test_detection_uses_running_not_active(self):
        # `module show` reports Status "Running" — "active" (the
        # systemctl vocabulary) never appears in the table.
        assert install._dongle_module_loaded(self._RUNNING_TABLE) is True
        assert install._dongle_module_loaded("0 modules loaded") is False


# ---------------------------------------------------------------------------
# _allowed_peers_yaml — own_ip must be in the allowlist (P4)
# ---------------------------------------------------------------------------

class TestAllowedPeersYaml:
    def test_distributed_includes_own_ip(self, ni_state):
        install.s.peer_ip = "100.95.195.40"
        install.s.own_ip = "100.124.155.106"
        assert install._allowed_peers_yaml() == \
            '    - "100.95.195.40"\n    - "100.124.155.106"'

    def test_single_dedupes_loopback(self, ni_state):
        install.s.peer_ip = "127.0.0.1"
        install.s.own_ip = "127.0.0.1"
        assert install._allowed_peers_yaml() == '    - "127.0.0.1"'

    def test_empty_ips_fall_back_to_loopback(self, ni_state):
        assert install._allowed_peers_yaml() == '    - "127.0.0.1"'

    def test_dedupes_when_ips_equal(self, ni_state):
        install.s.peer_ip = "100.64.0.2"
        install.s.own_ip = "100.64.0.2"
        assert install._allowed_peers_yaml() == '    - "100.64.0.2"'

# ---------------------------------------------------------------------------
# _merge_env — env file format: KEY=VALUE without spaces (systemd
# EnvironmentFile AND bash `source` both parse it; the legacy spaced
# "KEY = VALUE" broke `source`, and pre-existing spaced lines must
# self-normalize on the next installer run)
# ---------------------------------------------------------------------------

class TestMergeEnvFormat:
    def test_writes_key_eq_value_without_spaces(self, tmp_path, monkeypatch,
                                                ni_state):
        monkeypatch.setattr(install, "ENV_FILE", str(tmp_path / "env"))
        install.s.agent_token = "tok123"
        install.s.ami_pw = "pw456"
        install._merge_env()
        kv = [l for l in (tmp_path / "env").read_text().splitlines()
              if l and not l.startswith("#")]
        assert kv == ["SIMBRIDGE_AGENT_TOKEN=tok123",
                      "SIMBRIDGE_AMI_PASSWORD=pw456"]

    def test_spaced_legacy_lines_self_normalize(self, tmp_path, monkeypatch,
                                                ni_state):
        env = tmp_path / "env"
        env.write_text("SIMBRIDGE_AGENT_TOKEN = oldtok\n")
        monkeypatch.setattr(install, "ENV_FILE", str(env))
        # Same value: no value change, but the line is rewritten in the
        # canonical no-space format.
        install.s.agent_token = "oldtok"
        install._merge_env()
        text = env.read_text()
        assert "SIMBRIDGE_AGENT_TOKEN=oldtok" in text
        assert " = " not in text

    def test_extra_keys_survive(self, tmp_path, monkeypatch, ni_state):
        env = tmp_path / "env"
        env.write_text("EXTRA_KEY=keepme\n")
        monkeypatch.setattr(install, "ENV_FILE", str(env))
        install.s.http_secret = "hs789"
        install._merge_env()
        text = env.read_text()
        assert "EXTRA_KEY=keepme" in text
        assert "SIMBRIDGE_HTTP_SECRET=hs789" in text


# ---------------------------------------------------------------------------
# modules.conf — the module load list (S06.2, 2026-08-17 incident):
# the EPEL default lists modules Asterisk 18 no longer ships and omits
# res_pjsip's hard dependencies, so the PJSIP chain loaded silently
# broken and the voice bridge was dead with a "valid" pjsip.conf on disk.
# ---------------------------------------------------------------------------

def _mk_module_dir(tmp_path, omit=()):
    mdir = tmp_path / "modules"
    mdir.mkdir()
    for m in install.AST_MODULES_LOAD:
        if m not in omit:
            (mdir / m).touch()
    return mdir


class TestRenderModulesConf:
    def test_loads_only_installed_in_dependency_order(self):
        txt = install._render_modules_conf(set(install.AST_MODULES_LOAD))
        assert "[modules]" in txt
        assert "autoload=no" in txt
        i_sorcery = txt.index("res_sorcery_memory.so")
        i_pjsip = txt.index("load = res_pjsip.so")
        i_chan = txt.index("chan_pjsip.so")
        assert i_sorcery < i_pjsip < i_chan
        # Dead legacy entries from the EPEL default must not resurface.
        assert "res_pjsip_transport_udp" not in txt
        assert "res_pjsip_auth.so" not in txt

    def test_omits_missing_optional_module(self):
        installed = set(install.AST_MODULES_LOAD) - {"chan_dongle.so"}
        txt = install._render_modules_conf(installed)
        assert "chan_dongle.so" not in txt
        assert "chan_pjsip.so" in txt

    def test_aeap_core_loads_before_engine(self):
        # res_speech_aeap.so links ast_aeap_message_type_json from
        # res_aeap.so and the EPEL 18 loader does not auto-load
        # dependencies (live 2026-08-18, 3p14-aaa: undefined-symbol
        # load error) — the list order is load-bearing.
        txt = install._render_modules_conf(set(install.AST_MODULES_LOAD))
        assert txt.index("load = res_aeap.so") < \
            txt.index("load = res_speech_aeap.so")


class TestWriteModulesConf:
    def test_backs_up_and_marks_changed(self, tmp_path, monkeypatch,
                                        ni_state):
        mdir = _mk_module_dir(tmp_path)
        ast = tmp_path / "asterisk"
        ast.mkdir()
        (ast / "modules.conf").write_text("[modules]\nautoload=no\n")
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        monkeypatch.setattr(install, "AST_MODULE_DIRS", (str(mdir),))
        install.s.ast_modules_changed = False
        install._write_modules_conf()
        txt = (ast / "modules.conf").read_text()
        assert "autoload=no" in txt
        assert "load = chan_pjsip.so" in txt
        assert install.s.ast_modules_changed is True
        assert len(list(ast.glob("modules.conf.bak-*"))) == 1

    def test_unchanged_content_no_backup_no_flag(self, tmp_path,
                                                 monkeypatch, ni_state):
        mdir = _mk_module_dir(tmp_path)
        ast = tmp_path / "asterisk"
        ast.mkdir()
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        monkeypatch.setattr(install, "AST_MODULE_DIRS", (str(mdir),))
        install._write_modules_conf()
        first = (ast / "modules.conf").read_text()
        install.s.ast_modules_changed = False
        install._write_modules_conf()
        assert install.s.ast_modules_changed is False
        assert (ast / "modules.conf").read_text() == first
        assert not list(ast.glob("modules.conf.bak-*"))

    def test_missing_required_module_hard_exits(self, tmp_path, monkeypatch,
                                                ni_state, capsys):
        mdir = _mk_module_dir(tmp_path, omit={"res_pjsip.so"})
        ast = tmp_path / "asterisk"
        ast.mkdir()
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        monkeypatch.setattr(install, "AST_MODULE_DIRS", (str(mdir),))
        with pytest.raises(SystemExit) as e:
            install._write_modules_conf()
        assert e.value.code == 1
        assert "res_pjsip.so" in capsys.readouterr().err

    def test_missing_chan_dongle_is_not_fatal(self, tmp_path, monkeypatch,
                                              ni_state):
        mdir = _mk_module_dir(tmp_path, omit={"chan_dongle.so"})
        ast = tmp_path / "asterisk"
        ast.mkdir()
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        monkeypatch.setattr(install, "AST_MODULE_DIRS", (str(mdir),))
        install.s.ast_modules_changed = False
        install._write_modules_conf()  # must not raise
        assert "chan_dongle.so" not in (ast / "modules.conf").read_text()
        assert install.s.ast_modules_changed is True


# ---------------------------------------------------------------------------
# astagidir — the directory AGI() resolves scripts from (live finding
# 2026-08-18, 3p14-aaa): the EPEL default points at
# /usr/share/asterisk/agi-bin, an empty package dir, while the installer
# links the scripts into /usr/lib64/asterisk/agi-bin — scripts in a dir
# Asterisk does not scan are the same as missing scripts.
# ---------------------------------------------------------------------------

AGIDIR_TARGET = "/usr/lib64/asterisk/agi-bin"


class TestEnsureAgidir:
    def test_pins_wrong_default(self, tmp_path, monkeypatch, ni_state):
        ast = tmp_path / "asterisk"
        ast.mkdir()
        (ast / "asterisk.conf").write_text(
            "[directories]\nastagidir => /usr/share/asterisk/agi-bin\n"
        )
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        install.s.ast_core_changed = False
        install._ensure_agidir(AGIDIR_TARGET)
        txt = (ast / "asterisk.conf").read_text()
        assert f"astagidir => {AGIDIR_TARGET}" in txt
        assert "/usr/share/asterisk/agi-bin" not in txt
        # astagidir is a start-time core setting (main/options.c,
        # "core, can't reload") — the change must force a restart,
        # not a core reload
        assert install.s.ast_core_changed is True
        assert install.s.ast_config_changed is False
        # backup is created and keeps the pre-change value
        baks = list(ast.glob("asterisk.conf.bak-*"))
        assert len(baks) == 1
        assert "/usr/share/asterisk/agi-bin" in baks[0].read_text()

    def test_inserts_after_template_header(self, tmp_path, monkeypatch,
                                           ni_state):
        # the EPEL package file opens with the template header
        # "[directories](!)" — an equality match on "[directories]"
        # would miss it and drop the line into the wrong section.
        # The template header is ALSO a functional bug (see
        # test_normalizes_template_header_even_when_value_correct),
        # so the rewrite must leave a plain header behind.
        ast = tmp_path / "asterisk"
        ast.mkdir()
        (ast / "asterisk.conf").write_text(
            "[directories](!)\n"
            "; commented default\n"
            ";astagidir => /usr/share/asterisk/agi-bin\n"
            "[files]\n"
        )
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        install._ensure_agidir(AGIDIR_TARGET)
        lines = (ast / "asterisk.conf").read_text().splitlines()
        assert "(!)" not in "\n".join(lines)
        i_hdr = lines.index("[directories]")
        assert lines[i_hdr + 1] == f"astagidir => {AGIDIR_TARGET}"

    def test_normalizes_template_header_even_when_value_correct(
            self, tmp_path, monkeypatch, ni_state):
        # Live root cause (3p14-aaa, 2026-08-18): "[directories](!)"
        # marks the section as a TEMPLATE category — invisible to
        # ast_variable_browse() (main/config.c: an empty filter matches
        # !cat->ignored). With a CORRECT astagidir value AND a full
        # restart, the process still reported the build default. The
        # header must be normalized even when the value already matches.
        ast = tmp_path / "asterisk"
        ast.mkdir()
        (ast / "asterisk.conf").write_text(
            f"[directories](!)\nastagidir => {AGIDIR_TARGET}\n"
        )
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        install.s.ast_core_changed = False
        install._ensure_agidir(AGIDIR_TARGET)
        lines = (ast / "asterisk.conf").read_text().splitlines()
        assert lines[0] == "[directories]"
        assert f"astagidir => {AGIDIR_TARGET}" in lines
        assert install.s.ast_core_changed is True
        # the backup keeps the broken header for the rollback
        baks = list(ast.glob("asterisk.conf.bak-*"))
        assert len(baks) == 1
        assert "[directories](!)" in baks[0].read_text()
        # second run: plain header + correct value — no change, no new
        # backup, flag untouched (idempotency of the normalization)
        install.s.ast_core_changed = False
        install._ensure_agidir(AGIDIR_TARGET)
        assert (ast / "asterisk.conf").read_text().splitlines() == lines
        assert len(list(ast.glob("asterisk.conf.bak-*"))) == 1
        assert install.s.ast_core_changed is False

    def test_appends_section_when_absent(self, tmp_path, monkeypatch,
                                         ni_state):
        # the installer-created minimal conf has no [directories] at all
        ast = tmp_path / "asterisk"
        ast.mkdir()
        (ast / "asterisk.conf").write_text(
            "[files]\nastriskdir => /var/lib/asterisk\n"
        )
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        install._ensure_agidir(AGIDIR_TARGET)
        txt = (ast / "asterisk.conf").read_text()
        i_sec = txt.index("[directories]")
        assert f"astagidir => {AGIDIR_TARGET}" in txt[i_sec:]

    def test_unchanged_no_backup_keeps_flag(self, tmp_path, monkeypatch,
                                            ni_state):
        ast = tmp_path / "asterisk"
        ast.mkdir()
        conf = f"[directories]\nastagidir => {AGIDIR_TARGET}\n"
        (ast / "asterisk.conf").write_text(conf)
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        install.s.ast_core_changed = True  # a prior change survives
        install._ensure_agidir(AGIDIR_TARGET)
        assert (ast / "asterisk.conf").read_text() == conf
        assert not list(ast.glob("asterisk.conf.bak-*"))
        assert install.s.ast_core_changed is True

    def test_missing_file_warns_not_fatal(self, tmp_path, monkeypatch,
                                          ni_state, capsys):
        ast = tmp_path / "asterisk"
        ast.mkdir()
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        install._ensure_agidir(AGIDIR_TARGET)  # must not raise
        assert "astagidir" in capsys.readouterr().err
        assert not (ast / "asterisk.conf").exists()


class TestAgiDirFromConfig:
    def test_reads_active_line(self, tmp_path, monkeypatch):
        # the commented-out default (EPEL ships it as ";astagidir =>")
        # must not shadow the active line
        ast = tmp_path / "asterisk"
        ast.mkdir()
        (ast / "asterisk.conf").write_text(
            "[directories]\n;astagidir => /usr/share/asterisk/agi-bin\n"
            f"astagidir => {AGIDIR_TARGET}\n"
        )
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        assert install._agi_dir_from_config() == AGIDIR_TARGET

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        ast = tmp_path / "asterisk"
        ast.mkdir()
        monkeypatch.setattr(install, "AST_DIR", str(ast))
        assert install._agi_dir_from_config() == ""


# ---------------------------------------------------------------------------
# /run/lock — chan_dongle's lock files need sticky 1777 (live finding
# 2026-08-18, 3p14-aaa: the RHEL9 default 0755 broke lock_create() for
# the asterisk user). /run is tmpfs, so the mode is re-applied at boot
# from a tmpfiles.d entry.
# ---------------------------------------------------------------------------

class TestEnsureRunLock:
    def _patch(self, tmp_path, monkeypatch):
        lock = tmp_path / "lock"
        tf = tmp_path / "tmpfiles" / "run-lock.conf"
        monkeypatch.setattr(install, "RUN_LOCK_DIR", str(lock))
        monkeypatch.setattr(install, "RUN_LOCK_TMPFILES", str(tf))
        return lock, tf

    def test_fixes_mode_and_writes_tmpfiles(self, tmp_path, monkeypatch):
        lock, tf = self._patch(tmp_path, monkeypatch)
        lock.mkdir()
        lock.chmod(0o755)  # the RHEL9 default
        install._ensure_run_lock()
        assert (lock.stat().st_mode & 0o7777) == 0o1777
        assert f"d {lock} 1777 root root -" in tf.read_text()

    def test_idempotent_no_backup(self, tmp_path, monkeypatch):
        lock, tf = self._patch(tmp_path, monkeypatch)
        lock.mkdir()
        lock.chmod(0o1777)
        install._ensure_run_lock()
        first = tf.read_text()
        install._ensure_run_lock()
        assert tf.read_text() == first
        assert not list(tf.parent.glob("run-lock.conf.bak-*"))

    def test_missing_dir_still_persists_mode(self, tmp_path, monkeypatch):
        # /run may be absent (test box): the tmpfiles "d" type creates
        # the directory at boot, so the entry must still be written
        lock, tf = self._patch(tmp_path, monkeypatch)
        install._ensure_run_lock()  # must not raise
        assert tf.exists()
        assert f"d {lock} 1777 root root -" in tf.read_text()


class TestPhaseStartRestartPolicy:
    """modules.conf (like the unit EnvironmentFile) is only read at
    process start — a load-list change must restart Asterisk, not
    `core reload` (a reload applies nothing and the node keeps running
    the broken load list — the 2026-08-17 failure mode)."""

    def _prime(self):
        install.s.node_role = "gsm"
        install.s.action = "update"
        install.s.own_ip = "127.0.0.1"
        install.s.ast_env_changed = False
        install.s.ast_config_changed = False
        install.s.ast_modules_changed = True

    def test_modules_change_triggers_restart(self, monkeypatch, ni_state):
        self._prime()
        cmds = []

        def fake_run_ok(cmd):
            cmds.append(cmd)
            return "is-active" in cmd  # everything already active

        monkeypatch.setattr(install, "run_ok", fake_run_ok)
        # Never let the test touch real /etc on a box without Asterisk.
        monkeypatch.setattr(install, "_write_default_asterisk_conf",
                            lambda: None)
        install.phase_start()
        assert any("systemctl restart asterisk" in c for c in cmds)
        assert not any("core reload" in c for c in cmds)

    def test_config_only_change_stays_non_disruptive(self, monkeypatch,
                                                     ni_state):
        self._prime()
        install.s.ast_modules_changed = False
        install.s.ast_config_changed = True
        cmds = []

        def fake_run_ok(cmd):
            cmds.append(cmd)
            return "is-active" in cmd

        def fake_run_q(cmd, *a, **k):
            cmds.append(cmd)  # core reload goes through run_q
            return types.SimpleNamespace(
                returncode=0, stdout="reload complete\n")

        monkeypatch.setattr(install, "run_ok", fake_run_ok)
        monkeypatch.setattr(install, "run_q", fake_run_q)
        monkeypatch.setattr(install, "_write_default_asterisk_conf",
                            lambda: None)
        install.phase_start()
        assert not any("systemctl restart asterisk" in c for c in cmds)
        assert any("core reload" in c for c in cmds)

    def test_core_config_change_triggers_restart(self, monkeypatch,
                                                 ni_state):
        # astagidir and friends live in asterisk.conf [directories],
        # which main/options.c loads with "core, can't reload" —
        # a core reload would leave the old value in effect
        # (verified live 2026-08-18, 3p14-aaa).
        self._prime()
        install.s.ast_modules_changed = False
        install.s.ast_core_changed = True
        cmds = []

        def fake_run_ok(cmd):
            cmds.append(cmd)
            return "is-active" in cmd

        monkeypatch.setattr(install, "run_ok", fake_run_ok)
        monkeypatch.setattr(install, "_write_default_asterisk_conf",
                            lambda: None)
        install.phase_start()
        assert any("systemctl restart asterisk" in c for c in cmds)
        assert not any("core reload" in c for c in cmds)
