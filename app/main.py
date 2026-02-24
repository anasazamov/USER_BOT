from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

from telethon import TelegramClient

from app.admin_web import AdminWebServer
from app.config import Settings
from app.group_discovery import GroupDiscoveryManager
from app.invite_manager import InviteLinkManager
from app.keywords import KeywordService
from app.logging_setup import configure_logging
from app.management_bot import TelegramManagementBot
from app.message_queue import MessageQueue
from app.priority_groups import seed_priority_groups
from app.rate_limit import CooldownManager, InMemoryWindowLimiter
from app.runtime_config import RuntimeConfigService
from app.storage.db import ActionRepository, Postgres
from app.storage.redis_state import RedisWindowLimiter
from app.telegram_bot import build_userbot
from app.actions import ActionExecutor

logger = logging.getLogger(__name__)


def _spawn_userbot_task(name: str, userbot: object) -> asyncio.Task[None]:
    async def _runner() -> None:
        await userbot.start()

    task = asyncio.create_task(_runner(), name=name)

    def _done_callback(done_task: asyncio.Task[None]) -> None:
        with suppress(asyncio.CancelledError):
            exc = done_task.exception()
            if exc:
                logger.error(
                    "userbot_task_failed",
                    extra={"action": "userbot_task", "reason": name},
                    exc_info=exc,
                )

    task.add_done_callback(_done_callback)
    return task


async def _wait_until_any_userbot_stops(tasks: list[asyncio.Task[None]]) -> None:
    if not tasks:
        raise RuntimeError("no_userbot_tasks_started")
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        with suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc:
                raise exc
            raise RuntimeError(f"userbot_disconnected:{task.get_name()}")


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
    runtime = runtime_config.snapshot()
    logger.info(
        "runtime_config_effective",
        extra={
            "action": "runtime_config",
            "reason": (
                f"discovery_enabled={runtime.discovery_enabled} "
                f"join_limit_day={runtime.join_limit_day} "
                f"query_limit={runtime.discovery_query_limit} "
                f"join_batch={runtime.discovery_join_batch}"
            ),
        },
    )
    seeded_public_1, seeded_private_1 = await seed_priority_groups(repository, settings.priority_group_links)
    seeded_public_2, seeded_private_2 = await seed_priority_groups(repository, settings.priority_group_links_2)
    seeded_public = seeded_public_1 + seeded_public_2
    seeded_private = seeded_private_1 + seeded_private_2
    if seeded_public or seeded_private:
        logger.info(
            "priority_groups_seeded",
            extra={
                "action": "startup_seed",
                "reason": (
                    f"priority_groups_1={len(settings.priority_group_links)} "
                    f"priority_groups_2={len(settings.priority_group_links_2)}"
                ),
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
    management_bot: TelegramManagementBot | None = None
    if settings.bot_token:
        management_bot = TelegramManagementBot(settings=settings, repository=repository)

    executor = ActionExecutor(
        client,
        settings,
        cooldown,
        repository,
        runtime_config=runtime_config,
        bot_publisher=management_bot,
    )
    with suppress(Exception):
        await executor.refresh_private_invite_route_cache()
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
    userbot_tasks: list[asyncio.Task[None]] = []

    try:
        if web_server:
            await web_server.start()
        if management_bot:
            await management_bot.start()
        userbot_tasks.append(_spawn_userbot_task("userbot-primary", userbot))
        await asyncio.sleep(2.0)
        with suppress(Exception):
            await invite_manager.run_once()
        if discovery_manager:
            with suppress(Exception):
                await discovery_manager.run_once()
        await invite_manager.start()
        if discovery_manager:
            await discovery_manager.start()
        await _wait_until_any_userbot_stops(userbot_tasks)
    finally:
        if web_server:
            with suppress(Exception):
                await web_server.stop()
        if discovery_manager:
            with suppress(Exception):
                await discovery_manager.stop()
        if management_bot:
            with suppress(Exception):
                await management_bot.stop()
        with suppress(Exception):
            await invite_manager.stop()
        with suppress(Exception):
            await userbot.shutdown()
        for task in userbot_tasks:
            task.cancel()
        if userbot_tasks:
            with suppress(Exception):
                await asyncio.gather(*userbot_tasks, return_exceptions=True)
        await db.close()


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
            if not Settings.from_env().process_auto_restart:
                break
            logger.warning("process_exited_restart", extra={"action": "process", "reason": "main_returned"})
        except KeyboardInterrupt:
            break
        except Exception:
            logger.exception("process_crashed_restarting", extra={"action": "process", "reason": "crash"})
            try:
                restart_settings = Settings.from_env()
            except Exception:
                restart_settings = None
            if restart_settings and not restart_settings.process_auto_restart:
                raise
        try:
            restart_backoff = Settings.from_env().process_restart_backoff_sec
        except Exception:
            restart_backoff = 5
        time.sleep(max(1, int(restart_backoff)))
