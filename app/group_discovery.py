from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from telethon import TelegramClient, functions, types

from app.actions import ActionExecutor
from app.runtime_config import RuntimeConfigService
from app.storage.db import ActionRepository, DiscoveredGroup

logger = logging.getLogger(__name__)


class GroupDiscoveryManager:
    def __init__(
        self,
        client: TelegramClient,
        repository: ActionRepository,
        executor: ActionExecutor,
        queries: tuple[str, ...],
        interval_sec: int,
        query_limit: int,
        join_batch: int,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.executor = executor
        self.queries = queries
        self.interval_sec = interval_sec
        self.query_limit = query_limit
        self.join_batch = join_batch
        self.runtime_config = runtime_config
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="group-discovery-manager")

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
            if not await self._is_authorized():
                await asyncio.sleep(10)
                continue
            try:
                runtime = self.runtime_config.snapshot() if self.runtime_config else None
                if runtime and not runtime.discovery_enabled:
                    logger.info("group_discovery_skipped", extra={"action": "discovery", "reason": "disabled"})
                    await asyncio.sleep(self.interval_sec)
                    continue

                queries = runtime.discovery_queries if runtime else self.queries
                query_limit = runtime.discovery_query_limit if runtime else self.query_limit
                join_batch = runtime.discovery_join_batch if runtime else self.join_batch
                logger.info(
                    "group_discovery_iteration",
                    extra={"action": "discovery", "count": len(queries), "reason": f"limit={query_limit}"},
                )

                for query in queries:
                    await self._discover_query(query, query_limit=query_limit)
                await self._join_pending(join_batch)
            except Exception:
                logger.exception("group_discovery_iteration_failed")
            await asyncio.sleep(self.interval_sec)

    async def _discover_query(self, query: str, query_limit: int | None = None) -> None:
        limit = query_limit if query_limit is not None else self.query_limit
        result = await self.client(functions.contacts.SearchRequest(q=query, limit=limit))
        discovered = 0
        for chat in result.chats:
            if not isinstance(chat, types.Channel):
                continue
            if not (getattr(chat, "megagroup", False) or getattr(chat, "gigagroup", False)):
                continue

            username = chat.username if chat.username else None
            await self.repository.upsert_discovered_group(
                peer_id=int(chat.id),
                title=chat.title or "",
                username=username,
                source_query=query,
                joined=not chat.left,
            )
            discovered += 1
        logger.info(
            "group_discovery_query_done",
            extra={"action": "discovery_query", "reason": query, "count": discovered},
        )

    async def _join_pending(self, join_batch: int | None = None) -> None:
        limit = join_batch if join_batch is not None else self.join_batch
        pending = await self.repository.fetch_unjoined_public_groups(limit=limit)
        logger.info("group_discovery_join_batch", extra={"action": "join_public_batch", "count": len(pending)})
        for group in pending:
            joined = await self.executor.try_join_public(group.username, group.peer_id)
            if joined:
                await self.repository.mark_group_joined(group.peer_id)
                logger.info("joined_public_group", extra={"chat_id": group.peer_id, "action": "join_public"})
            else:
                await self.repository.mark_group_error(group.peer_id, "join_failed")

    async def _is_authorized(self) -> bool:
        try:
            return await self.client.is_user_authorized()
        except Exception:
            logger.debug("group_discovery_auth_check_failed")
            return False
