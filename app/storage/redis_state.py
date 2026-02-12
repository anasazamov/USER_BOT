from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

from app.rate_limit import LimitRule


@dataclass(slots=True)
class RedisWindowLimiter:
    redis: Redis

    @classmethod
    async def create(cls, redis_url: str) -> "RedisWindowLimiter":
        redis = Redis.from_url(redis_url, decode_responses=True)
        await redis.ping()
        return cls(redis=redis)

    async def allow(self, key: str, rule: LimitRule) -> bool:
        current = await self.redis.incr(key)
        if current == 1:
            await self.redis.expire(key, rule.window_seconds)
        return current <= rule.limit
