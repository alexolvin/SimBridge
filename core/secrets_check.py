"""Pre-commit hook: refuse to commit secrets, API hashes, phone numbers, etc.

Usage:
    cp scripts/pre-commit.sh .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit

Exit 0 = clean (commit allowed).
Exit 1 = secrets detected (commit blocked).
"""

# Patterns are defined here for testing; the shell hook calls this module
# via ``python -c`` or runs inline.

import re
import sys
from typing import NamedTuple


class SecretMatch(NamedTuple):
    file: str
    line: int
    pattern_name: str
    snippet: str


# (name, regex) — ordered from most specific to least specific
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("telegram_api_hash", re.compile(r"api[_-]?hash\s*[:=]\s*['\"][0-9a-f]{32}['\"]", re.I)),
    ("telegram_api_id", re.compile(r"api[_-]?id\s*[:=]\s*['\"]?\d{8,}['\"]?", re.I)),
    ("session_string", re.compile(r"1[a-gA-G]{200}")),  # Telethon session strings
    ("http_secret", re.compile(r"http[_-]?secret\s*[:=]\s*['\"][0-9a-f]{32}['\"]", re.I)),
    ("bearer_token", re.compile(r"bearer\s+[a-zA-Z0-9_\-]{32,}")),
    ("e164_phone", re.compile(r"(?<!\d)(\+7\d{10}|\+1\d{10})(?!\d)")),
    ("imei", re.compile(r"(?<!\d)(\d{15})(?!\d)")),  # 15-digit IMEI
    ("imsi", re.compile(r"(?<!\d)(\d{14,15})(?!\d)")),  # 14-15 digit IMSI
    ("session_file_ref", re.compile(r"\.session(?:-journal)?(?:\b|[/\"'])")),
    ("bare_telegram_id", re.compile(r"telegram_user_id\s*[:=]\s*['\"]?\d{8,}['\"]?", re.I)),
]


def scan_file(filepath: str) -> list[SecretMatch]:
    """Scan a single file for secret patterns. Return list of matches."""
    matches: list[SecretMatch] = []
    try:
        with open(filepath, errors="ignore") as fh:
            for lineno, line in enumerate(fh, 1):
                # Skip comment-only lines in test fixtures
                stripped = line.strip()
                if stripped.startswith("# [TEST_FIXTURE]") or stripped.startswith("# SECRET_CHECK_IGNORE"):
                    continue

                for name, pattern in _PATTERNS:
                    if pattern.search(line):
                        snippet = line.strip()[:80]
                        matches.append(SecretMatch(filepath, lineno, name, snippet))
    except (OSError, UnicodeDecodeError):
        pass  # binary or unreadable — skip
    return matches


def main() -> int:
    """Entry point. Scan files passed as args. Exit 1 if secrets found."""
    if len(sys.argv) < 2:
        print("Usage: secrets_check.py <file> [file ...]", file=sys.stderr)
        return 2

    all_matches: list[SecretMatch] = []
    for fp in sys.argv[1:]:
        all_matches.extend(scan_file(fp))

    if not all_matches:
        return 0

    print("COMMIT BLOCKED: potential secrets detected:", file=sys.stderr)
    for m in all_matches:
        print(f"  {m.file}:{m.line}  [{m.pattern_name}]  {m.snippet}", file=sys.stderr)
    print(
        f"\n{len(all_matches)} issue(s) found. Fix them or add "
        f"'# SECRET_CHECK_IGNORE' to the line above the false positive.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
