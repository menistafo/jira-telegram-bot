# rate_limit.py
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(slots=True)
class RateLimit:
    limit: int
    per_seconds: float


class RateLimiter:
    """Простой in-memory rate limiter (per-process).

    Важно:
      - в нескольких процессах/контейнерах лимиты не синхронизируются.
        Для этого нужен Redis. Но даже per-process лимит уже сильно снижает нагрузку.
    """

    def __init__(self) -> None:
        self._buckets: Dict[Tuple[int, str], Tuple[int, float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, user_id: int, key: str, rl: RateLimit) -> bool:
        """True если действие разрешено."""
        now = time.monotonic()
        bucket_key = (user_id, key)
        async with self._lock:
            used, reset_at = self._buckets.get(bucket_key, (0, now + rl.per_seconds))
            if now >= reset_at:
                used = 0
                reset_at = now + rl.per_seconds
            if used >= rl.limit:
                self._buckets[bucket_key] = (used, reset_at)
                return False
            self._buckets[bucket_key] = (used + 1, reset_at)
            return True

    async def retry_after(self, user_id: int, key: str) -> float:
        now = time.monotonic()
        async with self._lock:
            _, reset_at = self._buckets.get((user_id, key), (0, now))
            return max(0.0, reset_at - now)
