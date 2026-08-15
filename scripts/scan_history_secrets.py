#!/usr/bin/env python3
"""Scan the ENTIRE git history for secrets (S01.1 control; S06.4 TS06-11).

A secret removed in a later commit is still in the history. This tool:

  1. streams every object in the object database
     (``git cat-file --batch-all-objects`` — includes unreachable objects),
  2. runs the same pattern set as ``core/secrets_check.py`` over every blob,
  3. runs the filename check over every path a blob has ever had
     (``git rev-list --objects --all``),
  4. attributes each blocking hit to the commits that introduced it
     (``git log --find-object``).

The current tracked allowlist (``config/secret_check_allowlist.txt``) and the
per-line ``SECRET_CHECK_IGNORE`` token apply, so verified fakes do not
re-report on every run.

Usage:  python3 scripts/scan_history_secrets.py
Exit:   1 if any blocking hit, 0 if clean (warnings may be printed).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import secrets_check as sc  # noqa: E402


def git(*args: str) -> str:
    p = subprocess.run(["git", *args], capture_output=True, cwd=PROJECT_ROOT)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.decode()}")
    return p.stdout.decode(errors="replace")


def blob_paths() -> dict[str, set[str]]:
    """Map blob oid -> set of paths it has had in any ref."""
    mapping: dict[str, set[str]] = {}
    for line in git("rev-list", "--objects", "--all").splitlines():
        oid, _, path = line.partition(" ")
        if path:  # commits/trees have no path field
            mapping.setdefault(oid, set()).add(path)
    return mapping


def iter_blobs() -> "list[tuple[str, bytes]]":
    """All blobs in the ODB via one batch stream (type, size, content)."""
    p = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"],
        capture_output=True,
        cwd=PROJECT_ROOT,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode())
    data = p.stdout
    pos, blobs = 0, []
    while pos < len(data):
        eol = data.index(b"\n", pos)
        oid, otype, size_s = data[pos:eol].decode("ascii").split(" ")
        size = int(size_s)
        content = data[eol + 1 : eol + 1 + size]
        pos = eol + 1 + size + 1  # content + separator LF
        if otype == "blob":
            blobs.append((oid, content))
    return blobs


def commits_for(oid: str) -> str:
    out = git("log", "--all", "--format=%h %cs %s", "--find-object=" + oid).strip()
    return " ".join(out.splitlines()) or "(unreachable object)"


def main() -> int:
    allowlist = sc._load_allowlist()
    paths = blob_paths()

    blocking: list[tuple[str, str, int, str, str]] = []  # oid, path, line, pattern, snippet
    warn_counts: dict[str, int] = {}
    secret_filenames: list[tuple[str, str]] = []  # oid, path
    seen: set[tuple[str, int, str]] = set()

    blobs = iter_blobs()
    for oid, content in blobs:
        # Filename layer over every path this blob has had.
        for path in sorted(paths.get(oid, ())):
            if sc.is_secret_filename(path):
                secret_filenames.append((oid[:12], path))

        if sc.is_binary(content):
            continue
        lines = content.decode("utf-8", errors="ignore").splitlines()
        label = sorted(paths.get(oid, (oid[:12],)))[0]
        for m in sc.scan_lines(lines, label, allowlist):
            key = (oid, m.line, m.pattern_name)
            if key in seen:
                continue
            seen.add(key)
            if m.severity == "block":
                blocking.append((oid, label, m.line, m.pattern_name, m.snippet))
            else:
                warn_counts[m.pattern_name] = warn_counts.get(m.pattern_name, 0) + 1

    if secret_filenames:
        for oid, path in secret_filenames:
            print(f"  BLOCK  {oid}  [secret_filename]  {path}")
        print()

    if blocking:
        print(f"HISTORY SCAN: {len(blocking)} blocking hit(s) in git history:", file=sys.stderr)
        for oid, label, lineno, pattern, snippet in blocking:
            commits = commits_for(oid)
            print(f"  {label}:{lineno}  [{pattern}]  {snippet}", file=sys.stderr)
            print(f"      blob {oid[:12]} — commits: {commits}", file=sys.stderr)
    else:
        print(f"HISTORY CLEAN: no blocking secret hits across {len(blobs)} blobs "
              f"({len(paths)} unique blob paths).")

    if warn_counts:
        detail = ", ".join(f"{k}: {v}" for k, v in sorted(warn_counts.items()))
        print(f"warnings (non-blocking): {detail}")

    return 1 if (blocking or secret_filenames) else 0


if __name__ == "__main__":
    sys.exit(main())
