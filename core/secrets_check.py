"""Secret scanner for SimBridge (Rule 5 — secrets never enter git).

Used by:
  - the pre-commit hook (``scripts/pre-commit.sh``) on staged files
  - the git-history scan (``scripts/scan_history_secrets.py``) on every blob
  - unit tests (``tests/test_foundation.py``, ``tests/test_secrets_check.py``)

Two layers of defense
---------------------
1. **Filename check** — staging a file that *is* a secret (``*.session``,
   ``.env``, a private key, ...) is always blocked. This is the correct
   mechanism for "a session file": committing the file is the leak;
   *mentioning* its path in installer code, docs, or ``.gitignore`` is not.
2. **Content scan** — line patterns in two severity classes:
   - ``block``: high-confidence secret shapes (Telegram API hash/id, session
     string, HTTP secret, bearer token, IMEI/IMSI, E.164 phone, bare
     ``telegram_user_id``) — exit 1.
   - ``warn``:  low-confidence signals (references to ``.session`` paths) —
     printed, but the commit is allowed.

False-positive controls (no hardcoded values in the logic — Rule 1)
-------------------------------------------------------------------
- A line containing the token ``SECRET_CHECK_IGNORE`` is skipped (use it as a
  trailing comment on a line holding a verified fake).
- ``config/secret_check_allowlist.txt`` (tracked, hand-editable) lists values
  verified as fakes — spec-mandated example phone numbers, scanner self-test
  fixtures. A line containing any of them is skipped. A real leaked value must
  never be allowlisted — it must be removed from the repo (and history).
- Binary files (NUL byte in the first 8 KiB) are skipped.

Exit codes (CLI): 0 = no blocking hits (warnings may be printed),
1 = blocking hits found, 2 = usage error.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

SUPPRESS_TOKEN = "SECRET_CHECK_IGNORE"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = _PROJECT_ROOT / "config" / "secret_check_allowlist.txt"


class SecretMatch(NamedTuple):
    file: str
    line: int
    pattern_name: str
    severity: str  # "block" | "warn"
    snippet: str


# High-confidence secret shapes.
_BLOCKING_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("telegram_api_hash", re.compile(r"api[_-]?hash\s*[:=]\s*['\"][0-9a-f]{32}['\"]", re.I)),
    ("telegram_api_id", re.compile(r"api[_-]?id\s*[:=]\s*['\"]?\d{8,}['\"]?", re.I)),
    ("session_string", re.compile(r"1[a-gA-G]{200}")),
    ("http_secret", re.compile(r"http[_-]?secret\s*[:=]\s*['\"][0-9a-f]{32}['\"]", re.I)),
    ("bearer_token", re.compile(r"bearer\s+[a-zA-Z0-9_\-]{32,}")),
    ("e164_phone", re.compile(r"(?<!\d)(\+7\d{10}|\+1\d{10})(?!\d)")),
    # 15 digits: IMEI or IMSI — indistinguishable by length alone.
    ("imei", re.compile(r"(?<!\d)(\d{15})(?!\d)")),
    # 14 digits: IMSI without check digit.
    ("imsi", re.compile(r"(?<!\d)(\d{14})(?!\d)")),
    ("bare_telegram_id", re.compile(r"telegram_user_id\s*[:=]\s*['\"]?\d{8,}['\"]?", re.I)),
]

# Low-confidence signals: reported as warnings, never block.
_WARNING_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("session_file_ref", re.compile(r"\.session(?:-journal)?(?:\b|[/\"'])")),
]

# Filenames that are themselves secrets — staging one is always blocked.
_SECRET_FILE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\.session(-journal)?$"),
    re.compile(r"^\.env(\..+)?$"),
    re.compile(r"\.(key|pem|p12|pfx|ppk)$", re.I),
    re.compile(r"^id_(rsa|ecdsa|ed25519)$"),
    re.compile(r"\.secret$"),
]


def _load_allowlist(path: Path | None = None) -> list[str]:
    """Load verified-fake values from the tracked allowlist file."""
    p = path or ALLOWLIST_PATH
    if not p.exists():
        return []
    values: list[str] = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            values.append(line)
    return values


def is_binary(data: bytes) -> bool:
    """True for binary content.

    NUL in the first 8 KiB, or invalid UTF-8 anywhere. The UTF-8 check is
    needed because binary audio (e.g. u-law silence = 0x7F runs) can be
    NUL-free, while every real text file in this repo is valid UTF-8
    (English/Russian docs, code, configs).
    """
    if b"\x00" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def scan_lines(
    lines: list[str],
    filepath: str,
    allowlist: list[str] | None = None,
) -> list[SecretMatch]:
    """Scan lines of text. ``allowlist=None`` loads the tracked allowlist."""
    if allowlist is None:
        allowlist = _load_allowlist()
    matches: list[SecretMatch] = []
    for lineno, line in enumerate(lines, 1):
        if SUPPRESS_TOKEN in line:
            continue
        if any(value in line for value in allowlist):
            continue
        for name, pattern in _BLOCKING_PATTERNS:
            if pattern.search(line):
                matches.append(
                    SecretMatch(filepath, lineno, name, "block", line.strip()[:80])
                )
        for name, pattern in _WARNING_PATTERNS:
            if pattern.search(line):
                matches.append(
                    SecretMatch(filepath, lineno, name, "warn", line.strip()[:80])
                )
    return matches


def scan_file(
    filepath: str,
    allowlist: list[str] | None = None,
) -> list[SecretMatch]:
    """Scan a single file. Binary and unreadable files are skipped."""
    try:
        data = Path(filepath).read_bytes()
    except OSError:
        return []
    if is_binary(data):
        return []
    return scan_lines(data.decode("utf-8", errors="ignore").splitlines(), filepath, allowlist)


def is_secret_filename(filename: str) -> bool:
    """True if the file (by name) is a secret: session file, .env, private key."""
    name = filename.rsplit("/", 1)[-1]
    return any(p.search(name) for p in _SECRET_FILE_PATTERNS)


def secret_filename_matches(files: list[str]) -> list[str]:
    """Filter a file list down to the entries that are secret filenames."""
    return [f for f in files if is_secret_filename(f)]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Scan files (filename + content). Exit 1 on blocking."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("Usage: secrets_check.py <file> [file ...]", file=sys.stderr)
        return 2

    bad_files = secret_filename_matches(argv)
    allowlist = _load_allowlist()
    all_matches: list[SecretMatch] = []
    for fp in argv:
        all_matches.extend(scan_file(fp, allowlist))

    blocking = [m for m in all_matches if m.severity == "block"]
    warnings = [m for m in all_matches if m.severity == "warn"]

    for m in warnings:
        print(f"  warning  {m.file}:{m.line}  [{m.pattern_name}]  {m.snippet}", file=sys.stderr)

    if bad_files or blocking:
        print("COMMIT BLOCKED: potential secrets detected:", file=sys.stderr)
        for f in bad_files:
            print(f"  {f}  [secret_filename]  staged file is a secret file type", file=sys.stderr)
        for m in blocking:
            print(f"  {m.file}:{m.line}  [{m.pattern_name}]  {m.snippet}", file=sys.stderr)
        print(
            f"\n{len(bad_files) + len(blocking)} blocking issue(s), {len(warnings)} warning(s). "
            f"Put '{SUPPRESS_TOKEN}' on a line holding a verified fake, "
            f"or add the value to config/secret_check_allowlist.txt.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
