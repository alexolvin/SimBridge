"""Per-user phonebooks — dial-by-name and caller ID.

Each Telegram user keeps their own phonebook, managed with the /add and
/del commands (no file import); entries are stored per user and used for:
- /call <name> — name -> number resolution before dialing;
- /sms <name> — name -> number resolution for outgoing SMS;
- incoming-event identification (SMS) — the recipient's own directory is
  consulted before the global contacts cache.

Storage: ONE JSON file on the Telegram node, keyed by Telegram user ID::

    {"449550030": [{"name": "Ivanov_Ivan", "number": "+79261234555"}]}

Design notes:
- hot-reload by mtime (same discipline as ACL / contacts cache) so an
  out-of-band edit or a second writer is picked up without restart;
- atomic writes (tmp file + os.replace) — a crash never leaves a
  truncated store;
- pure stdlib, no network I/O, thread-safe via a single lock;
- names are stored with spaces replaced by underscores (single-token),
  so a name never contains a whitespace.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional

from core.phone import normalize_e164

logger = logging.getLogger("simbridge.user_contacts")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class UserContactsStore:
    """Per-user phonebook store (JSON file, hot-reload, atomic writes).

    *path* is the store file. A missing file means "no directories yet"
    (the store starts empty and is created on first save).
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: Dict[str, List[Dict[str, str]]] = {}
        self._mtime: float = 0
        self._reload()

    def _reload(self) -> None:
        """Re-read the store file if its mtime advanced (hot-reload)."""
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return
        if st.st_mtime <= self._mtime:
            return
        try:
            with open(self._path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("store root must be an object")
        except (ValueError, OSError) as e:
            # A corrupt store must not take the userbot down — keep the
            # last good in-memory copy and log (the file stays for repair).
            logger.error("user contacts store %s unreadable, keeping cache: %s",
                         self._path, e)
            return
        clean: Dict[str, List[Dict[str, str]]] = {}
        for uid, entries in loaded.items():
            if not isinstance(entries, list):
                continue
            entries = [
                e for e in entries
                if isinstance(e, dict) and e.get("name") and e.get("number")
            ]
            if entries:
                clean[str(uid)] = entries
        with self._lock:
            self._data = clean
            self._mtime = st.st_mtime
        logger.info("Reloaded user contacts store: %d users, %d entries",
                    len(clean), sum(len(v) for v in clean.values()))

    # -- read API ----------------------------------------------------------

    def get(self, user_id: int) -> List[Dict[str, str]]:
        self._reload()
        with self._lock:
            return [dict(e) for e in self._data.get(str(user_id), [])]

    def count(self, user_id: int) -> int:
        return len(self.get(user_id))

    def users(self) -> List[int]:
        self._reload()
        with self._lock:
            return sorted(int(u) for u in self._data)

    def resolve_number(self, user_id: int, number: str) -> Optional[str]:
        """Display name for *number* from the user's own directory."""
        if not number:
            return None
        norm = normalize_e164(number)
        for e in self.get(user_id):
            if e["number"] == number or (norm and e["number"] == norm):
                return e["name"]
        return None

    def find_by_name(self, user_id: int, query: str) -> List[Dict[str, str]]:
        """Case-insensitive name search, tiered: exact > prefix > substring.

        Tiers are returned in that order; a non-empty earlier tier
        suppresses later ones (deterministic picks for /call).
        """
        q = (query or "").strip().casefold()
        if not q:
            return []
        entries = self.get(user_id)
        exact = [e for e in entries if e["name"].casefold() == q]
        if exact:
            return exact
        prefix = [e for e in entries if e["name"].casefold().startswith(q)]
        if prefix:
            return prefix
        return [e for e in entries if q in e["name"].casefold()]

    # -- write API ---------------------------------------------------------

    def add_unique(self, user_id: int, name: str, number: str) -> tuple[bool, str, str]:
        """Add ONE entry, rejecting a duplicate number OR name.

        Names are stored with spaces replaced by underscores (single-token).
        Returns ``(ok, reason, detail)``:
          - ``(True, "ok", "<normalized_number>")`` — added;
          - ``(False, "number_exists", "<existing_name>")``;
          - ``(False, "name_exists", "<existing_number>")``;
          - ``(False, "invalid", "no name" | "bad number")``.
        """
        name = str(name or "").strip().replace(" ", "_")
        number = str(number or "").strip()
        if not name:
            return (False, "invalid", "no name")
        norm = normalize_e164(number)
        if not norm:
            return (False, "invalid", "bad number")
        entries = self.get(user_id)
        for e in entries:
            if (normalize_e164(e["number"]) or e["number"]) == norm:
                return (False, "number_exists", e["name"])
        for e in entries:
            if e["name"].casefold() == name.casefold():
                return (False, "name_exists", e["number"])
        entries.append({"name": name, "number": norm})
        self.replace_for(user_id, entries)
        logger.info("user contacts: user %s added %s (%s)", user_id, name, norm)
        return (True, "ok", norm)

    def delete_by_number(self, user_id: int, number: str) -> int:
        """Remove all of the user's entries with this number (exact,
        E.164-normalized match both ways). Returns the removed count."""
        if not number:
            return 0
        norm = normalize_e164(number)
        entries = self.get(user_id)
        kept = [
            e for e in entries
            if not (e["number"] == number or (norm and e["number"] == norm))
        ]
        removed = len(entries) - len(kept)
        if removed:
            self.replace_for(user_id, kept)
        return removed

    def delete_by_name(self, user_id: int, name: str) -> int:
        """Remove the user's entries with this EXACT (case-insensitive)
        name. Destructive operation, hence no fuzzy matching — the
        caller resolves ambiguity first (find_by_name)."""
        target = (name or "").strip().casefold()
        if not target:
            return 0
        entries = self.get(user_id)
        kept = [e for e in entries if e["name"].casefold() != target]
        removed = len(entries) - len(kept)
        if removed:
            self.replace_for(user_id, kept)
        return removed

    def replace_for(self, user_id: int, entries: List[Dict[str, str]]) -> int:
        """Replace the user's directory with *entries*; return the count.

        Atomic on disk: the new JSON is written to a temp file in the
        same directory and os.replace()'d over the store.
        """
        clean = [
            {"name": str(e["name"]).strip(), "number": str(e["number"]).strip()}
            for e in entries
            if str(e.get("name", "")).strip() and str(e.get("number", "")).strip()
        ]
        with self._lock:
            if clean:
                self._data[str(user_id)] = clean
            else:
                self._data.pop(str(user_id), None)
            snapshot = {k: list(v) for k, v in self._data.items()}

        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".user_contacts.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False, indent=1)
                fh.write("\n")
            os.replace(tmp, str(self._path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # Adopt the new mtime so the next _reload does not treat our own
        # write as an external change (it would only re-read the same data).
        try:
            with self._lock:
                self._mtime = self._path.stat().st_mtime
        except OSError:
            pass
        logger.info("user contacts: user %s directory replaced, %d entries",
                    user_id, len(clean))
        return len(clean)
