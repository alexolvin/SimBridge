"""Blacklist manager — atomic file writes, plain text format.

The blacklist file is the single source of truth (Rule 1). Format:
- One E.164 number per line
- `#` comments supported
- Hand-editable

Writes are atomic: write to temp file, then rename. A partial write
can never leave the file unreadable because the dialplan greps this
file on every incoming call.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Optional, Set

from core.phone import normalize_e164

logger = logging.getLogger("simbridge.blacklist")


class BlacklistManager:
    """Thread-safe blacklist with atomic file writes.

    Hot-reload: ``contains``/``block``/``unblock`` detect file changes
    automatically (mtime-based), so hand edits to the blacklist file take
    effect without a restart.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._numbers: Set[str] = set()
        self._mtime: float = 0.0
        self._load()
        self._mtime = self._stat_mtime()

    def _stat_mtime(self) -> float:
        """Return the file's mtime, or 0.0 if it does not exist."""
        try:
            return os.stat(self._path).st_mtime
        except OSError:
            return 0.0

    def _maybe_reload(self) -> None:
        """Reload from disk if the file changed since the last check.

        No-op when the mtime is unchanged. Must be called WITHOUT holding
        ``self._lock`` (it acquires the lock itself).
        """
        mtime = self._stat_mtime()
        if mtime == self._mtime:
            return
        with self._lock:
            if mtime == self._mtime:
                return  # another thread already reloaded
            self._numbers.clear()
            self._load()
            self._mtime = self._stat_mtime()

    def _load(self) -> None:
        """Load all numbers from the blacklist file."""
        try:
            with open(self._path, encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    norm = normalize_e164(stripped)
                    if norm:
                        self._numbers.add(norm)
        except FileNotFoundError:
            pass  # empty blacklist is fine

    def contains(self, number: str) -> bool:
        """Check if *number* is blacklisted."""
        norm = normalize_e164(number)
        self._maybe_reload()
        with self._lock:
            return norm in self._numbers if norm else False

    def block(self, number: str) -> bool:
        """Add *number* to the blacklist.

        Returns True if the number was newly added, False if already present.
        Write is atomic (temp file + rename).
        """
        norm = normalize_e164(number)
        if not norm:
            logger.warning("Cannot block malformed number: %s", number)
            return False

        self._maybe_reload()
        with self._lock:
            if norm in self._numbers:
                return False
            self._numbers.add(norm)
            self._write()
            logger.info("Blocked number: %s", norm)
            return True

    def unblock(self, number: str) -> bool:
        """Remove *number* from the blacklist.

        Returns True if the number was removed, False if not present.
        """
        norm = normalize_e164(number)
        if not norm:
            return False

        self._maybe_reload()
        with self._lock:
            if norm not in self._numbers:
                return False
            self._numbers.discard(norm)
            self._write()
            logger.info("Unblocked number: %s", norm)
            return True

    def _write(self) -> None:
        """Atomically write the blacklist file.

        Write to a temp file in the same directory, then rename.
        This ensures the dialplan's grep never sees a partial write.
        """
        dir_name = os.path.dirname(self._path)
        os.makedirs(dir_name, exist_ok=True)

        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=dir_name, prefix=".blacklist_", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("# SimBridge blacklist — auto-generated\n")
                fh.write(f"# Last updated: {datetime.now(timezone.utc).isoformat()}\n")
                for num in sorted(self._numbers):
                    fh.write(f"{num}\n")
            os.replace(tmp_path, self._path)
            # Refresh the tracked mtime so _maybe_reload does not
            # re-read the file we just wrote.
            self._mtime = self._stat_mtime()
        except OSError:
            # Cleanup temp file on error
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            raise

    def reload(self) -> None:
        """Force reload from disk (e.g., after manual edit)."""
        with self._lock:
            self._numbers.clear()
            self._load()
            self._mtime = self._stat_mtime()

    @property
    def count(self) -> int:
        self._maybe_reload()
        with self._lock:
            return len(self._numbers)
