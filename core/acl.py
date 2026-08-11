"""ACL (Access Control List) manager.

Reads ``acl.conf`` — format: ``<telegram_user_id> <right1> <right2> ...``
One entry per line. Unknown IDs are denied by default.

Supports hot-reload: call ``reload()`` to re-read the file without restarting.
"""

from __future__ import annotations

import hashlib
import os
import threading
from typing import Optional


_RIGHTS = {"in_sms", "in_call", "out_sms", "out_call"}


class ACLManager:
    """Thread-safe ACL manager with hot-reload."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._rules: dict[int, set[str]] = {}
        self._mtime: float = 0
        self.reload()

    def reload(self) -> None:
        """Re-read the ACL file. Skips if file hasn't changed."""
        try:
            st = os.stat(self._path)
        except FileNotFoundError:
            return

        if st.st_mtime == self._mtime:
            return  # no change

        rules: dict[int, set[str]] = {}
        with open(self._path) as fh:
            for lineno, raw_line in enumerate(fh, 1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue  # skip malformed lines
                try:
                    uid = int(parts[0])
                except ValueError:
                    continue  # skip non-numeric IDs
                rights = set(parts[1:]) & _RIGHTS
                rules[uid] = rights

        with self._lock:
            self._rules = rules
            self._mtime = st.st_mtime

    def check(self, uid: int, right: str) -> bool:
        """Check whether *uid* has *right*. Default: deny."""
        if right not in _RIGHTS:
            return False
        with self._lock:
            return right in self._rules.get(uid, set())

    def get_user_rights(self, uid: int) -> set[str]:
        """Return all rights for *uid* (empty set if unknown)."""
        with self._lock:
            return self._rules.get(uid, set()).copy()

    @property
    def user_count(self) -> int:
        with self._lock:
            return len(self._rules)
