from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from app.config import Settings
from app.storage.db import ActionRepository, BotSubscriber

logger = logging.getLogger(__name__)


class TelegramManagementBot:
    def __init__(self, settings: Settings, repository: ActionRepository) -> None:
        token = (settings.bot_token or "").strip()
        if not token:
            raise ValueError("bot_token_required")
        self.repository = repository
        self.token = token
        self.poll_timeout_sec = max(5, int(settings.bot_poll_timeout_sec))
        self.broadcast_enabled = bool(settings.bot_broadcast_subscribers)
        self.admin_user_ids = set(settings.bot_admin_user_ids)
        self._api_base = f"https://api.telegram.org/bot{self.token}"
        self._offset = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._session: ClientSession | None = None

    async def start(self) -> None:
        if self._task:
            return
        self._session = ClientSession(timeout=ClientTimeout(total=self.poll_timeout_sec + 20))
        self._task = asyncio.create_task(self._run(), name="telegram-management-bot")
        logger.info(
            "management_bot_started",
            extra={
                "action": "bot_manage",
                "reason": (
                    f"admins={len(self.admin_user_ids)} "
                    f"broadcast_subscribers={self.broadcast_enabled}"
                ),
            },
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None

    async def send_message(self, chat_id: str | int, text: str) -> int:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        result = await self._api_call("sendMessage", payload)
        return int((result or {}).get("message_id") or 0)

    async def edit_message(self, chat_id: str | int, message_id: int, text: str) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        await self._api_call("editMessageText", payload)

    async def broadcast_to_subscribers(self, text: str) -> tuple[int, int]:
        if not self.broadcast_enabled:
            return (0, 0)
        sent = 0
        failed = 0
        subscribers = await self.repository.fetch_bot_subscribers(limit=5000, active_only=True)
        for subscriber in subscribers:
            try:
                await self.send_message(subscriber.chat_id, text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception as exc:
                failed += 1
                if self._is_permanent_subscriber_error(str(exc)):
                    await self.repository.set_bot_subscriber_active(subscriber.user_id, False)
        return sent, failed

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                updates = await self._fetch_updates()
                for update in updates:
                    update_id = int(update.get("update_id", 0) or 0)
                    if update_id > 0:
                        self._offset = update_id + 1
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("management_bot_loop_failed")
                await asyncio.sleep(2)

    async def _fetch_updates(self) -> list[dict[str, Any]]:
        payload = {
            "timeout": self.poll_timeout_sec,
            "offset": self._offset,
            "allowed_updates": ["message"],
        }
        result = await self._api_call("getUpdates", payload)
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return []

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        from_user = message.get("from") or {}
        chat = message.get("chat") or {}
        user_id = int(from_user.get("id") or 0)
        chat_id = int(chat.get("id") or 0)
        if user_id <= 0 or chat_id == 0:
            return
        chat_type = str(chat.get("type") or "")
        username = from_user.get("username")
        first_name = from_user.get("first_name")

        command, arg = self._parse_command(text)
        if command in {"start", "subscribe"} and chat_type == "private":
            await self.repository.upsert_bot_subscriber(
                user_id=user_id,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                active=True,
            )
            await self.send_message(chat_id, self._welcome_text())
            return

        if command in {"stop", "unsubscribe"} and chat_type == "private":
            updated = await self.repository.set_bot_subscriber_active(user_id, False)
            if not updated:
                await self.repository.upsert_bot_subscriber(
                    user_id=user_id,
                    chat_id=chat_id,
                    username=username,
                    first_name=first_name,
                    active=False,
                )
            await self.send_message(chat_id, "Obuna to'xtatildi. Qayta yoqish: /start")
            return

        if command == "help":
            await self.send_message(chat_id, self._help_text())
            return

        if command == "stats":
            if not self._is_admin(user_id):
                await self.send_message(chat_id, "Ruxsat yo'q.")
                return
            await self.send_message(chat_id, await self._build_stats_text())
            return

        if command in {"subscribers", "subs"}:
            if not self._is_admin(user_id):
                await self.send_message(chat_id, "Ruxsat yo'q.")
                return
            await self.send_message(chat_id, await self._build_subscribers_text())
            return

        if command == "broadcast":
            if not self._is_admin(user_id):
                await self.send_message(chat_id, "Ruxsat yo'q.")
                return
            if not arg:
                await self.send_message(chat_id, "Matn bering: /broadcast <xabar>")
                return
            sent, failed = await self.broadcast_to_subscribers(arg)
            await self.send_message(chat_id, f"Broadcast yakunlandi. Sent={sent} Failed={failed}")
            return

        await self.send_message(chat_id, self._help_text())

    async def _build_stats_text(self) -> str:
        stats = await self.repository.fetch_action_stats()
        subscribers_active = await self.repository.count_bot_subscribers(active_only=True)
        subscribers_total = await self.repository.count_bot_subscribers(active_only=False)
        lines = [
            "Bot statistikasi:",
            f"Publish (1h): {stats.published_1h}",
            f"Publish (24h): {stats.published_24h}",
            f"Edit (24h): {stats.edited_24h}",
            f"Join (24h): {stats.joins_24h}",
            f"Error (24h): {stats.errors_24h}",
            f"Total action (24h): {stats.total_actions_24h}",
            f"Subscribers active/total: {subscribers_active}/{subscribers_total}",
        ]
        return "\n".join(lines)

    async def _build_subscribers_text(self) -> str:
        subscribers = await self.repository.fetch_bot_subscribers(limit=20, active_only=False)
        active_count = await self.repository.count_bot_subscribers(active_only=True)
        total_count = await self.repository.count_bot_subscribers(active_only=False)
        if not subscribers:
            return f"Subscriberlar yo'q. Active/total: {active_count}/{total_count}"
        lines = [f"Subscribers active/total: {active_count}/{total_count}", "Oxirgi 20 ta:"]
        for subscriber in subscribers:
            lines.append(self._subscriber_line(subscriber))
        return "\n".join(lines)

    @staticmethod
    def _subscriber_line(subscriber: BotSubscriber) -> str:
        username = f"@{subscriber.username}" if subscriber.username else "-"
        status = "active" if subscriber.active else "inactive"
        return f"{subscriber.user_id} {username} {status}"

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_user_ids

    @staticmethod
    def _parse_command(text: str) -> tuple[str, str]:
        parts = text.strip().split(maxsplit=1)
        command_token = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        command = command_token[1:]
        if "@" in command:
            command = command.split("@", 1)[0]
        return command.lower(), arg

    @staticmethod
    def _welcome_text() -> str:
        return (
            "Obuna muvaffaqiyatli yoqildi.\n"
            "Buyruqlar:\n"
            "/start - obunani yoqish\n"
            "/stop - obunani to'xtatish\n"
            "/help - yordam"
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "Buyruqlar:\n"
            "/start\n"
            "/stop\n"
            "/help\n"
            "/stats (admin)\n"
            "/subscribers (admin)\n"
            "/broadcast <text> (admin, BOT_BROADCAST_SUBSCRIBERS=true bo'lsa ishlaydi)"
        )

    @staticmethod
    def _is_permanent_subscriber_error(error_text: str) -> bool:
        lowered = error_text.lower()
        return (
            "bot was blocked by the user" in lowered
            or "chat not found" in lowered
            or "forbidden" in lowered
            or "user is deactivated" in lowered
        )

    async def _api_call(self, method: str, payload: dict[str, Any]) -> Any:
        if not self._session:
            raise RuntimeError("management_bot_not_started")
        url = f"{self._api_base}/{method}"
        async with self._session.post(url, json=payload) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        if not data.get("ok"):
            description = str(data.get("description") or "unknown_error")
            raise RuntimeError(f"telegram_bot_api_error:{description}")
        return data.get("result")
