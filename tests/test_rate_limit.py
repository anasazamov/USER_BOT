import asyncio

from app.rate_limit import InMemoryWindowLimiter, LimitRule


def test_window_limiter_blocks_after_limit() -> None:
    limiter = InMemoryWindowLimiter()

    async def run() -> None:
        assert await limiter.allow("k", LimitRule(2, 60)) is True
        assert await limiter.allow("k", LimitRule(2, 60)) is True
        assert await limiter.allow("k", LimitRule(2, 60)) is False

    asyncio.run(run())
