from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol


@dataclass(slots=True)
class LimitRule:
    limit: int
    window_seconds: int


class WindowLimiter(Protocol):
    async def allow(self, key: str, rule: LimitRule) -> bool:
        ...


class InMemoryWindowLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str, rule: LimitRule) -> bool:
        now = datetime.now(timezone.utc)
        floor = now - timedelta(seconds=rule.window_seconds)
        async with self._lock:
            q = self._events[key]
            while q and q[0] < floor:
                q.popleft()
            if len(q) >= rule.limit:
                return False
            q.append(now)
            return True


class CooldownManager:
    def __init__(self, limiter: WindowLimiter) -> None:
        self.limiter = limiter

    async def allow_action(self, chat_id: int, action: str, limit: int, window: int) -> bool:
        key = f"chat:{chat_id}:action:{action}"
        return await self.limiter.allow(key, LimitRule(limit=limit, window_seconds=window))

    async def allow_global(self, action: str, limit: int, window: int) -> bool:
        key = f"global:action:{action}"
        return await self.limiter.allow(key, LimitRule(limit=limit, window_seconds=window))

    async def allow_join(self, limit: int) -> bool:
        return await self.limiter.allow("account:join", LimitRule(limit=limit, window_seconds=86400))
