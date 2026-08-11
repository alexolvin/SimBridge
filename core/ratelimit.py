"""Token-bucket rate limiter per key.

Used to enforce ``limits.sms_per_hour`` and ``limits.calls_per_minute``.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class RateLimiter:
    """Sliding-window rate limiter per key.

    Parameters
    ----------
    max_requests: maximum number of requests per window
    window_seconds: window duration in seconds
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._lock = threading.Lock()
        self._buckets: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        """Return ``True`` if the request is allowed, ``False`` if rate-limited."""
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            timestamps = self._buckets.get(key, [])
            # Remove expired entries
            timestamps = [t for t in timestamps if t > cutoff]

            if len(timestamps) >= self._max:
                self._buckets[key] = timestamps
                return False

            timestamps.append(now)
            self._buckets[key] = timestamps
            return True

    def remaining(self, key: str) -> int:
        """Return how many requests *key* can still make in the current window."""
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            timestamps = self._buckets.get(key, [])
            timestamps = [t for t in timestamps if t > cutoff]
            return max(0, self._max - len(timestamps))
