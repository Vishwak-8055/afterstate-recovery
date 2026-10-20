from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window limiter. In-process: with several API replicas the effective limit is per replica
    (documented limitation; put a shared limiter at the proxy for strict global limits)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(window_s - (now - q[0])) + 1)
            q.append(now)
            if len(self._hits) > 20_000:
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > window_s][:10_000]:
                    self._hits.pop(k, None)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
