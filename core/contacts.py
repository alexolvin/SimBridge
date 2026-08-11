"""Contact name resolution — cache-only, never network on SMS path.

Lookup order:
1. Local CSV cache (paths.contacts_cache)
2. Built-in service number directory (900, 112, etc.)
3. Bare E.164 number (no name)

The ContactProvider interface allows additional sources (e.g., Google
Contacts sync) to be plugged in later without restructuring.
"""

from __future__ import annotations

import csv
import logging
import threading
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from pathlib import Path

from core.phone import normalize_e164

logger = logging.getLogger("simbridge.contacts")


# Built-in service numbers (Russian + universal)
_SERVICE_NUMBERS: Dict[str, str] = {
    "+74957877777": "SberBank",
    "+74957777777": "SberBank Info",
    "900": "Городской справочный",
    "112": "Единая служба спасения",
    "101": "Полиция",
    "102": "Пожарные",
    "103": "Скорая",
    "104": "Газ служба",
}


class ContactProvider(ABC):
    """Abstract interface for contact lookups.

    Implementations must be cache-only — no synchronous network calls
    on the SMS path.
    """

    @abstractmethod
    def lookup(self, number: str) -> Optional[str]:
        """Return display name for *number* (already normalized to E.164).

        Returns None if not found. Must not make network calls.
        """


class ServiceNumberProvider(ContactProvider):
    """Built-in service number directory."""

    def lookup(self, number: str) -> Optional[str]:
        # Try raw first for short service numbers (they won't normalize to E.164)
        raw_hit = _SERVICE_NUMBERS.get(number)
        if raw_hit:
            return raw_hit
        norm = normalize_e164(number)
        if norm:
            return _SERVICE_NUMBERS.get(norm)
        return None


class CSVContactProvider(ContactProvider):
    """Local CSV cache: number,name — hand-editable.

    Format:
    ```
    # comments start with #
    +79261234555,Иванов Иван Иванович
    +14155552671,Acme Corp
    ```

    Auto-reloads when the file mtime changes (hot-reload, like ACL).
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._cache: Dict[str, str] = {}
        self._mtime: float = 0
        self._reload()

    def _reload(self) -> None:
        """Re-read the CSV file. Skips if unchanged."""
        try:
            st = self._path.stat()
        except FileNotFoundError:
            return

        if st.st_mtime <= self._mtime:
            return

        cache: Dict[str, str] = {}
        with open(self._path, newline="", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split(",", 1)
                if len(parts) < 2:
                    continue
                num = normalize_e164(parts[0].strip())
                name = parts[1].strip()
                if num and name:
                    cache[num] = name
                elif name:
                    # Non-E.164 (e.g., short number)
                    raw = parts[0].strip()
                    cache[raw] = name

        with self._lock:
            self._cache = cache
            self._mtime = st.st_mtime
        logger.info("Reloaded contacts cache: %d entries", len(cache))

    def lookup(self, number: str) -> Optional[str]:
        norm = normalize_e164(number) if number else None
        with self._lock:
            # Try normalized first, then raw
            return self._cache.get(norm) if norm else self._cache.get(number, None)

    def reload(self) -> None:
        """Force reload regardless of mtime."""
        with self._lock:
            self._mtime = 0
        self._reload()


class ContactResolver:
    """Composed resolver: local cache → service numbers → bare number.

    Never makes a network call. If you need Google Contacts sync,
    implement a ContactProvider and add it to the chain.
    """

    def __init__(
        self,
        csv_path: str,
        extra_providers: Optional[List[ContactProvider]] = None,
    ) -> None:
        self._providers: List[ContactProvider] = [
            CSVContactProvider(csv_path),
        ]
        if extra_providers:
            self._providers.extend(extra_providers)
        self._providers.append(ServiceNumberProvider())

    def resolve(self, number: str) -> Optional[str]:
        """Resolve *number* to a display name.

        Returns the first non-None result from the provider chain,
        or None if no provider knows the number.
        """
        for provider in self._providers:
            name = provider.lookup(number)
            if name:
                return name
        return None
