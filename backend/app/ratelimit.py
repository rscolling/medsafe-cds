"""In-process token-bucket rate limiter keyed by client IP (single-instance prototype).

For multiple replicas use a shared store (Redis) or the API gateway; documented in docs/architecture.md.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RateLimiter:
    MAX_BUCKETS = 10_000

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        if self.capacity <= 0:
            return True, 0.0
        now = self._clock()
        with self._lock:
            if len(self._buckets) >= self.MAX_BUCKETS and key not in self._buckets:
                self._evict(now)
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, (1.0 - tokens) / self.rate

    def _evict(self, now: float) -> None:
        """Bound memory: drop buckets that have fully refilled (idle clients); if that is not enough, the oldest."""
        full = [k for k, (t, last) in self._buckets.items() if t + (now - last) * self.rate >= self.capacity]
        for k in full:
            del self._buckets[k]
        if len(self._buckets) >= self.MAX_BUCKETS:
            for k in sorted(self._buckets, key=lambda k: self._buckets[k][1])[: self.MAX_BUCKETS // 10]:
                del self._buckets[k]
