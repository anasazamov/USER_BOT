from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from telethon import TelegramClient

from app.actions import ActionExecutor
from app.storage.db import ActionRepository

logger = logging.getLogger(__name__)


class InviteLinkManager:
    def __init__(
        self,
        repository: ActionRepository,
        executor: ActionExecutor,
        client: TelegramClient,
        interval_sec: int,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.client = client
        self.interval_sec = interval_sec
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="invite-link-manager")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            if not self.client.is_connected():
                await asyncio.sleep(10)
                continue
            try:
                links = await self.repository.fetch_active_invite_links()
                for link in links:
                    joined = await self.executor.try_join(link)
                    if joined:
                        logger.info("joined_private_group", extra={"action": "join"})
            except Exception:
                logger.exception("invite_manager_iteration_failed")
            await asyncio.sleep(self.interval_sec)
