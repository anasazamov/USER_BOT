from __future__ import annotations

import asyncio
from app.models import NormalizedMessage


class MessageQueue:
    def __init__(self, max_size: int) -> None:
        self._queue: asyncio.Queue[NormalizedMessage] = asyncio.Queue(maxsize=max_size)

    async def put(self, item: NormalizedMessage) -> None:
        await self._queue.put(item)

    async def get(self, timeout: float | None = None) -> NormalizedMessage:
        if timeout is None:
            return await self._queue.get()
        return await asyncio.wait_for(self._queue.get(), timeout=timeout)

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    def qsize(self) -> int:
        return self._queue.qsize()
