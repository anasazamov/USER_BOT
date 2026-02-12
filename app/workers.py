from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.message_queue import MessageQueue
from app.models import NormalizedMessage

logger = logging.getLogger(__name__)

Processor = Callable[[NormalizedMessage], Awaitable[None]]


class WorkerPool:
    def __init__(
        self,
        queue: MessageQueue,
        processor: Processor,
        worker_count: int,
        poll_timeout: float,
    ) -> None:
        self.queue = queue
        self.processor = processor
        self.worker_count = worker_count
        self.poll_timeout = poll_timeout
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        for idx in range(self.worker_count):
            task = asyncio.create_task(self._worker(idx), name=f"worker-{idx}")
            self._tasks.append(task)

    async def stop(self) -> None:
        self._stop.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _worker(self, idx: int) -> None:
        logger.info("worker_started", extra={"action": "worker_start", "worker": idx})
        while not self._stop.is_set():
            try:
                item = await self.queue.get(timeout=self.poll_timeout)
            except TimeoutError:
                continue
            except asyncio.TimeoutError:
                continue

            try:
                logger.info(
                    "worker_message_received",
                    extra={
                        "action": "worker_receive",
                        "worker": idx,
                        "chat_id": item.envelope.chat_id,
                        "message_id": item.envelope.message_id,
                        "queue_size": self.queue.qsize(),
                    },
                )
                await self.processor(item)
                logger.info(
                    "worker_message_processed",
                    extra={
                        "action": "worker_processed",
                        "worker": idx,
                        "chat_id": item.envelope.chat_id,
                        "message_id": item.envelope.message_id,
                    },
                )
            except Exception:
                logger.exception("worker_failed")
            finally:
                self.queue.task_done()
