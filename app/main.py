from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from telethon import TelegramClient

from app.admin_web import AdminWebServer
from app.config import Settings
from app.group_discovery import GroupDiscoveryManager
from app.invite_manager import InviteLinkManager
from app.keywords import KeywordService
from app.logging_setup import configure_logging
from app.message_queue import MessageQueue
from app.priority_groups import seed_priority_groups
from app.rate_limit import CooldownManager, InMemoryWindowLimiter
from app.runtime_config import RuntimeConfigService
from app.storage.db import ActionRepository, Postgres
from app.storage.redis_state import RedisWindowLimiter
from app.telegram_bot import build_userbot
from app.actions import ActionExecutor

logger = logging.getLogger(__name__)


async def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    db = Postgres(settings.database_url)
    await db.connect()
    await db.apply_schema()
    repository = ActionRepository(db)
    keyword_service = KeywordService(repository)
    await keyword_service.initialize()
    runtime_config = RuntimeConfigService(settings, repository)
    await runtime_config.initialize()
    seeded_public, seeded_private = await seed_priority_groups(repository, settings.priority_group_links)
    if seeded_public or seeded_private:
        logger.info(
            "priority_groups_seeded",
            extra={
                "action": "startup_seed",
                "reason": "priority_groups",
                "count": seeded_public + seeded_private,
            },
        )

    limiter_backend = InMemoryWindowLimiter()
    if settings.redis_url:
        try:
            limiter_backend = await RedisWindowLimiter.create(settings.redis_url)
        except Exception:
            logger.exception("redis_unavailable_fallback_memory")

    cooldown = CooldownManager(limiter_backend)
    queue = MessageQueue(settings.queue_max_size)

    client = TelegramClient(settings.session_name, settings.api_id, settings.api_hash)
    executor = ActionExecutor(client, settings, cooldown, repository, runtime_config=runtime_config)
    invite_manager = InviteLinkManager(repository, executor, client, settings.invite_sync_interval_sec)
    web_server: AdminWebServer | None = None
    if settings.admin_web_enabled:
        web_server = AdminWebServer(
            settings=settings,
            keyword_service=keyword_service,
            repository=repository,
            runtime_config=runtime_config,
        )
    discovery_manager: GroupDiscoveryManager | None = None
    if settings.discovery_enabled:
        discovery_manager = GroupDiscoveryManager(
            client=client,
            repository=repository,
            executor=executor,
            queries=settings.discovery_queries,
            interval_sec=settings.discovery_interval_sec,
            query_limit=settings.discovery_query_limit,
            join_batch=settings.discovery_join_batch,
            runtime_config=runtime_config,
        )
    userbot = build_userbot(
        settings,
        client,
        queue,
        executor,
        repository,
        keyword_service,
        runtime_config=runtime_config,
    )

    try:
        if web_server:
            await web_server.start()
        await invite_manager.start()
        if discovery_manager:
            await discovery_manager.start()
        await userbot.start()
    finally:
        if web_server:
            with suppress(Exception):
                await web_server.stop()
        if discovery_manager:
            with suppress(Exception):
                await discovery_manager.stop()
        with suppress(Exception):
            await invite_manager.stop()
        await userbot.shutdown()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
