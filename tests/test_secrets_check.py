"""Tests for core.secrets_check — binary skip, suppression, allowlist,
filename layer, severities, and the CLI exit codes.

Complements tests/test_foundation.py (TS01-1 pattern detection, TS01-2 hook).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import secrets_check as sc


# =========================================================================
# Binary skip
# =========================================================================

class TestBinarySkip:
    def test_binary_file_produces_no_matches(self, tmp_path: Path):
        f = tmp_path / "voice.ulaw"
        # u-law audio: binary bytes, includes runs that look like digits.
        f.write_bytes(b"\x00\x01\x223456789012345\x00rest")
        assert sc.is_binary(f.read_bytes())
        assert sc.scan_file(str(f), allowlist=[]) == []

    def test_nulfree_binary_detected_via_utf8(self):
        """u-law silence is 0x7F runs (valid code points, no NULs) mixed with
        other audio bytes that form invalid UTF-8 — must still be binary."""
        data = b"\x7f\x7f\x89\xfa\x89" * 100
        assert b"\x00" not in data
        assert sc.is_binary(data)

    def test_cyrillic_text_is_not_binary(self):
        """Russian docs are valid UTF-8 with many high bytes — must be text."""
        data = "Не коммитить секреты, ключи, `.env`, `*.session`.\n" * 100
        assert not sc.is_binary(data.encode("utf-8"))

    def test_text_file_is_scanned(self, tmp_path: Path):
        f = tmp_path / "a.py"
        f.write_text('API_HASH = "0123456789abcdef0123456789abcdef"\n')
        matches = sc.scan_file(str(f), allowlist=[])
        assert [m.pattern_name for m in matches] == ["telegram_api_hash"]


# =========================================================================
# Per-line suppression token
# =========================================================================

class TestSuppressionToken:
    def test_trailing_token_suppresses_line(self):
        lines = ['API_HASH = "0123456789abcdef0123456789abcdef"  # SECRET_CHECK_IGNORE']
        assert sc.scan_lines(lines, "a.py", allowlist=[]) == []

    def test_token_does_not_suppress_other_lines(self):
        lines = [
            "# SECRET_CHECK_IGNORE  <- marker on its own line",
            'API_HASH = "0123456789abcdef0123456789abcdef"',
        ]
        matches = sc.scan_lines(lines, "a.py", allowlist=[])
        assert len(matches) == 1
        assert matches[0].line == 2


# =========================================================================
# Allowlist (config/secret_check_allowlist.txt)
# =========================================================================

class TestAllowlist:
    def _write_allowlist(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "allowlist.txt"
        p.write_text(content)
        return p

    def test_allowlisted_value_skips_line(self, tmp_path: Path):
        allow = self._write_allowlist(tmp_path, "# comment\n+79261234555\n")
        lines = ['sms = "+79261234555: hello"', 'other = "+79998887766"']
        matches = sc.scan_lines(lines, "a.py", sc._load_allowlist(allow))
        assert [m.line for m in matches] == [2]

    def test_missing_allowlist_file_is_empty(self, tmp_path: Path):
        assert sc._load_allowlist(tmp_path / "nope.txt") == []

    def test_real_repo_allowlist_covers_spec_example(self):
        """The spec-mandated example number must not block normal commits."""
        allow = sc._load_allowlist()
        assert "+79261234555" in allow
        assert sc.scan_lines(['x = "+79261234555"'], "a.py", allow) == []

    def test_unlisted_number_still_blocks(self):
        allow = sc._load_allowlist()
        matches = sc.scan_lines(['x = "+79267523624"'], "a.py", allow)
        assert [m.pattern_name for m in matches] == ["e164_phone"]
        assert matches[0].severity == "block"


# =========================================================================
# Filename layer (staging a secret file is always blocked)
# =========================================================================

class TestSecretFilenames:
    @pytest.mark.parametrize(
        "name",
        [
            "bot.session",
            "sim_session.session",
            "sim_session.session-journal",
            ".env",
            ".env.production",
            "id_rsa",
            "server.key",
            "cert.pem",
            "vault.p12",
            "agent.secret",
        ],
    )
    def test_secret_names_detected(self, name):
        assert sc.is_secret_filename(name)

    @pytest.mark.parametrize(
        "name",
        [
            "bot.py",
            "session.py",           # a module named session — not a session file
            "README.md",
            "id_rsa.pub",           # public keys are fine to commit
            "simbridge.yaml",
            "config/secret_check_allowlist.txt",
        ],
    )
    def test_non_secret_names_pass(self, name):
        assert not sc.is_secret_filename(name)

    def test_dotenv_examples_are_blocked(self):
        """Project convention (S01.2): config is YAML; secrets are env-var
        NAMES in the YAML, values via EnvironmentFile. There is no tracked
        dotenv template in this project, so any .env* file is suspect."""
        assert sc.is_secret_filename(".env.example")

    def test_secret_filename_with_directory(self):
        assert sc.is_secret_filename("/var/lib/simbridge/sim_session.session")
        assert sc.secret_filename_matches(
            ["agent/main.py", "bot.session", ".env"]
        ) == ["bot.session", ".env"]


# =========================================================================
# Severities
# =========================================================================

class TestSeverities:
    def test_session_path_reference_is_warning_only(self):
        """Installer code legitimately references the session path — warn, don't block."""
        lines = ['sess = Path("/var/lib/simbridge/sim_session.session")']
        matches = sc.scan_lines(lines, "install.py", allowlist=[])
        assert [(m.pattern_name, m.severity) for m in matches] == [
            ("session_file_ref", "warn"),
        ]

    def test_inlined_session_string_still_blocks(self):
        key = "1" + "a" * 200
        matches = sc.scan_lines([f'x = "{key}"'], "a.py", allowlist=[])
        assert any(m.pattern_name == "session_string" and m.severity == "block" for m in matches)

    def test_imei_and_imsi_are_mutually_exclusive(self):
        imei = sc.scan_lines(["x = 351451208401234"], "a.py", allowlist=[])
        assert [m.pattern_name for m in imei] == ["imei"]
        imsi = sc.scan_lines(["x = 35145089012345"], "a.py", allowlist=[])
        assert [m.pattern_name for m in imsi] == ["imsi"]


# =========================================================================
# CLI exit codes
# =========================================================================

class TestCli:
    def test_clean_files_exit_0(self, tmp_path: Path, capsys):
        f = tmp_path / "clean.py"
        f.write_text("x = 1\n")
        assert sc.main([str(f)]) == 0

    def test_blocking_hit_exit_1(self, tmp_path: Path, capsys):
        # Value must NOT be in the tracked allowlist (main() loads it).
        f = tmp_path / "leak.py"
        f.write_text('API_HASH = "ffffffffffffffffffffffffffffffff"\n')
        assert sc.main([str(f)]) == 1
        assert "COMMIT BLOCKED" in capsys.readouterr().err

    def test_warning_only_exit_0(self, tmp_path: Path, capsys):
        f = tmp_path / "paths.py"
        f.write_text('sess = Path("/var/lib/simbridge/sim_session.session")\n')
        assert sc.main([str(f)]) == 0
        err = capsys.readouterr().err
        assert "warning" in err and "COMMIT BLOCKED" not in err

    def test_secret_filename_exit_1(self, tmp_path: Path, capsys):
        f = tmp_path / "bot.session"
        f.write_text("anything")
        assert sc.main([str(f)]) == 1
        assert "secret_filename" in capsys.readouterr().err

    def test_no_args_exit_2(self):
        assert sc.main([]) == 2
