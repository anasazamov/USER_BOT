from __future__ import annotations

import asyncio
import datetime as _dt
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
        active_hour_utc_start: int = 18,
        active_hour_utc_end: int = 2,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.client = client
        self.interval_sec = interval_sec
        self.active_hour_utc_start = active_hour_utc_start
        self.active_hour_utc_end = active_hour_utc_end
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @staticmethod
    def _is_in_active_window(now_utc_hour: int, start: int, end: int) -> bool:
        if start == end:
            return True
        if start < end:
            return start <= now_utc_hour < end
        return now_utc_hour >= start or now_utc_hour < end

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="invite-link-manager")

    async def run_once(self) -> None:
        await self._run_iteration()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            ran = await self._run_iteration()
            await asyncio.sleep(self.interval_sec if ran else 10)

    async def _run_iteration(self) -> bool:
        if not self.client.is_connected():
            return False
        if not await self._is_authorized():
            return False
        # Stay off the Telethon socket during taxi peak hours; private-invite
        # imports get long FloodWaits which block the connection.
        now_utc_hour = _dt.datetime.now(_dt.timezone.utc).hour
        if not self._is_in_active_window(
            now_utc_hour, self.active_hour_utc_start, self.active_hour_utc_end
        ):
            logger.info(
                "invite_manager_off_hours",
                extra={
                    "action": "invite_scan",
                    "reason": (
                        f"now_utc_hour={now_utc_hour} "
                        f"active_window_utc={self.active_hour_utc_start}-{self.active_hour_utc_end}"
                    ),
                },
            )
            return True
        try:
            remaining = float(getattr(self.executor, "import_invite_floodwait_remaining", 0) or 0)
            if remaining > 0:
                logger.info(
                    "invite_iteration_skipped",
                    extra={
                        "action": "invite_scan",
                        "reason": f"import_invite_floodwait_remaining={int(remaining)}s",
                    },
                )
                return True
            links = await self.repository.fetch_active_invite_links()
            logger.info("invite_iteration", extra={"action": "invite_scan", "count": len(links)})
            for link in links:
                if float(getattr(self.executor, "import_invite_floodwait_remaining", 0) or 0) > 0:
                    # Got floodwait mid-iteration — stop hammering, resume next tick.
                    logger.info(
                        "invite_iteration_aborted",
                        extra={"action": "invite_scan", "reason": "floodwait_during_iteration"},
                    )
                    break
                joined = await self.executor.try_join(link)
                if joined:
                    logger.info("joined_private_group", extra={"action": "join", "reason": link[:120]})
        except Exception:
            logger.exception("invite_manager_iteration_failed")
        return True

    async def _is_authorized(self) -> bool:
        try:
            return await self.client.is_user_authorized()
        except Exception:
            logger.debug("invite_manager_auth_check_failed")
            return False
