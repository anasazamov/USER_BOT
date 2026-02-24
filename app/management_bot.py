from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
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
        self.paid_subscription_enabled = bool(settings.bot_paid_subscription_enabled)
        self.subscription_default_days = max(1, int(settings.bot_subscription_default_days))
        self.subscription_reminder_hours = max(1, int(settings.bot_subscription_reminder_hours))
        self.subscription_check_interval_sec = max(30, int(settings.bot_subscription_check_interval_sec))
        self.managed_private_group_ids = set(int(v) for v in settings.bot_managed_private_group_ids)
        self.auto_approve_join_requests = bool(settings.bot_auto_approve_join_requests)
        self.decline_unpaid_join_requests = bool(settings.bot_decline_unpaid_join_requests)
        self.remove_expired_from_groups = bool(settings.bot_remove_expired_from_groups)
        self._api_base = f"https://api.telegram.org/bot{self.token}"
        self._offset = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._session: ClientSession | None = None
        self._last_subscription_maintenance_monotonic = 0.0

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
                    f"broadcast_subscribers={self.broadcast_enabled} "
                    f"paid_subscription={self.paid_subscription_enabled} "
                    f"managed_private_groups={len(self.managed_private_group_ids)}"
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

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = await self._api_call("sendMessage", payload)
        return int((result or {}).get("message_id") or 0)

    async def edit_message(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
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
                await self._maybe_run_subscription_maintenance()
                updates = await self._fetch_updates()
                for update in updates:
                    update_id = int(update.get("update_id", 0) or 0)
                    if update_id > 0:
                        self._offset = update_id + 1
                    await self._handle_update(update)
                await self._maybe_run_subscription_maintenance()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("management_bot_loop_failed")
                await asyncio.sleep(2)

    async def _fetch_updates(self) -> list[dict[str, Any]]:
        payload = {
            "timeout": self.poll_timeout_sec,
            "offset": self._offset,
            "allowed_updates": ["message", "chat_join_request", "callback_query"],
        }
        result = await self._api_call("getUpdates", payload)
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return []

    async def _handle_update(self, update: dict[str, Any]) -> None:
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            await self._handle_callback_query(callback_query)
            return

        join_request = update.get("chat_join_request")
        if isinstance(join_request, dict):
            await self._handle_chat_join_request(join_request)
            return

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
        is_admin = self._is_admin(user_id)

        command, arg = self._parse_command(text)
        if command in {"start", "subscribe"} and chat_type == "private":
            existing = await self.repository.fetch_bot_subscriber_by_user_id(user_id)
            keep_active = bool(existing and self._has_active_access(existing))
            await self.repository.upsert_bot_subscriber(
                user_id=user_id,
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                active=keep_active or (not self.paid_subscription_enabled),
            )
            if self.paid_subscription_enabled and not keep_active:
                await self.repository.mark_bot_subscriber_pending(user_id)
                await self.send_message(
                    chat_id,
                    self._welcome_pending_text(default_days=self.subscription_default_days),
                    reply_markup=self._user_panel_keyboard(is_admin=is_admin),
                )
            else:
                await self.send_message(
                    chat_id,
                    self._welcome_text(),
                    reply_markup=self._user_panel_keyboard(is_admin=is_admin),
                )
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
            await self.send_message(
                chat_id,
                "Obuna to'xtatildi. Qayta yoqish: /start",
                reply_markup=self._user_panel_keyboard(is_admin=is_admin),
            )
            return

        if command == "status" and chat_type == "private":
            subscriber = await self.repository.fetch_bot_subscriber_by_user_id(user_id)
            await self.send_message(
                chat_id,
                self._build_subscriber_status_text(subscriber),
                reply_markup=self._user_panel_keyboard(is_admin=is_admin),
            )
            return

        if command in {"menu", "admin"} and chat_type == "private":
            if command == "admin" and not is_admin:
                await self.send_message(chat_id, "Ruxsat yo'q.")
                return
            await self.send_message(
                chat_id,
                self._admin_panel_text() if is_admin else self._user_panel_text(),
                reply_markup=self._admin_panel_keyboard() if is_admin else self._user_panel_keyboard(False),
            )
            return

        if command == "help":
            await self.send_message(
                chat_id,
                self._help_text(),
                reply_markup=self._user_panel_keyboard(is_admin=is_admin) if chat_type == "private" else None,
            )
            return

        if command == "stats":
            if not is_admin:
                await self.send_message(chat_id, "Ruxsat yo'q.")
                return
            await self.send_message(chat_id, await self._build_stats_text())
            return

        if command in {"subscribers", "subs"}:
            if not is_admin:
                await self.send_message(chat_id, "Ruxsat yo'q.")
                return
            await self.send_message(chat_id, await self._build_subscribers_text())
            return

        if command == "pending":
            if not is_admin:
                await self.send_message(chat_id, "Ruxsat yo'q.")
                return
            pending_text, pending_keyboard = await self._build_pending_panel(page=0)
            await self.send_message(chat_id, pending_text, reply_markup=pending_keyboard)
            return

        if command in {"approve", "extend"}:
            if not is_admin:
                await self.send_message(chat_id, "Ruxsat yo'q.")
                return
            target_user_id, days = self._parse_admin_extend_args(arg, self.subscription_default_days)
            if target_user_id is None:
                await self.send_message(
                    chat_id,
                    "Format: /approve (user_id) [kun]\nFormat: /extend (user_id) (kun)",
                )
                return
            subscriber = await self.repository.activate_or_extend_bot_subscriber_subscription(
                user_id=target_user_id,
                days=days,
                admin_user_id=user_id,
            )
            if subscriber is None:
                await self.send_message(chat_id, "Subscriber topilmadi. Avval user /start bossin.")
                return
            await self.send_message(chat_id, self._build_admin_extend_result_text(subscriber, days))
            if subscriber.chat_id:
                with suppress(Exception):
                    await self.send_message(
                        subscriber.chat_id,
                        self._subscription_approved_user_text(subscriber, days),
                    )
            return

        if command in {"checksubs", "subcheck"}:
            if not is_admin:
                await self.send_message(chat_id, "Ruxsat yo'q.")
                return
            await self._run_subscription_maintenance()
            await self.send_message(chat_id, "Subscription tekshiruvi ishga tushirildi.")
            return

        if command == "broadcast":
            if not is_admin:
                await self.send_message(chat_id, "Ruxsat yo'q.")
                return
            if not arg:
                await self.send_message(chat_id, "Matn bering: /broadcast (xabar)")
                return
            sent, failed = await self.broadcast_to_subscribers(arg)
            await self.send_message(chat_id, f"Broadcast yakunlandi. Sent={sent} Failed={failed}")
            return

        await self.send_message(chat_id, self._help_text())

    async def answer_callback_query(self, callback_query_id: str, text: str = "", alert: bool = False) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:180]
        if alert:
            payload["show_alert"] = True
        with suppress(Exception):
            await self._api_call("answerCallbackQuery", payload)

    async def _handle_callback_query(self, payload: dict[str, Any]) -> None:
        callback_id = str(payload.get("id") or "")
        data = str(payload.get("data") or "").strip()
        from_user = payload.get("from") or {}
        user_id = int(from_user.get("id") or 0)
        message = payload.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id") or 0)
        message_id = int(message.get("message_id") or 0)

        if not callback_id or not data:
            return

        parts = data.split(":")
        scope = parts[0] if parts else ""
        is_admin = self._is_admin(user_id)

        if scope == "usr":
            await self._handle_user_callback(
                callback_id=callback_id,
                user_id=user_id,
                chat_id=chat_id,
                message_id=message_id,
                parts=parts,
                is_admin=is_admin,
            )
            return

        if scope == "adm":
            if not is_admin:
                await self.answer_callback_query(callback_id, "Ruxsat yo'q", alert=True)
                return
            await self._handle_admin_callback(
                callback_id=callback_id,
                user_id=user_id,
                chat_id=chat_id,
                message_id=message_id,
                parts=parts,
            )
            return

        await self.answer_callback_query(callback_id, "Noma'lum amal")

    async def _handle_user_callback(
        self,
        callback_id: str,
        user_id: int,
        chat_id: int,
        message_id: int,
        parts: list[str],
        is_admin: bool,
    ) -> None:
        if chat_id == 0 or message_id == 0:
            await self.answer_callback_query(callback_id, "Xabar topilmadi")
            return
        action = parts[1] if len(parts) > 1 else "menu"
        if action == "status":
            subscriber = await self.repository.fetch_bot_subscriber_by_user_id(user_id)
            await self._edit_callback_message(
                chat_id,
                message_id,
                self._build_subscriber_status_text(subscriber),
                self._user_panel_keyboard(is_admin=is_admin),
            )
            await self.answer_callback_query(callback_id, "Holat yangilandi")
            return
        if action == "help":
            await self._edit_callback_message(
                chat_id,
                message_id,
                self._help_text(),
                self._user_panel_keyboard(is_admin=is_admin),
            )
            await self.answer_callback_query(callback_id, "Yordam")
            return
        if action == "menu":
            text = self._admin_panel_text() if is_admin else self._user_panel_text()
            keyboard = self._admin_panel_keyboard() if is_admin else self._user_panel_keyboard(False)
            await self._edit_callback_message(chat_id, message_id, text, keyboard)
            await self.answer_callback_query(callback_id, "Menyu")
            return
        await self.answer_callback_query(callback_id, "Noma'lum amal")

    async def _handle_admin_callback(
        self,
        callback_id: str,
        user_id: int,
        chat_id: int,
        message_id: int,
        parts: list[str],
    ) -> None:
        if chat_id == 0 or message_id == 0:
            await self.answer_callback_query(callback_id, "Xabar topilmadi")
            return
        action = parts[1] if len(parts) > 1 else "menu"
        if action == "menu":
            await self._edit_callback_message(chat_id, message_id, self._admin_panel_text(), self._admin_panel_keyboard())
            await self.answer_callback_query(callback_id, "Admin panel")
            return
        if action == "stats":
            await self._edit_callback_message(
                chat_id,
                message_id,
                await self._build_stats_text(),
                self._admin_panel_keyboard(),
            )
            await self.answer_callback_query(callback_id, "Statistika")
            return
        if action == "subs":
            await self._edit_callback_message(
                chat_id,
                message_id,
                await self._build_subscribers_text(),
                self._admin_panel_keyboard(),
            )
            await self.answer_callback_query(callback_id, "Subscriberlar")
            return
        if action == "check":
            await self._run_subscription_maintenance()
            text, keyboard = await self._build_pending_panel(page=0)
            await self._edit_callback_message(chat_id, message_id, text, keyboard)
            await self.answer_callback_query(callback_id, "Tekshiruv bajarildi")
            return
        if action == "pending":
            page = self._safe_int(parts[2] if len(parts) > 2 else "0", 0)
            text, keyboard = await self._build_pending_panel(page=page)
            await self._edit_callback_message(chat_id, message_id, text, keyboard)
            await self.answer_callback_query(callback_id, "Pending ro'yxat")
            return
        if action == "apr":
            target_user_id = self._safe_int(parts[2] if len(parts) > 2 else "0", 0)
            days = self._safe_int(parts[3] if len(parts) > 3 else str(self.subscription_default_days), 0)
            page = self._safe_int(parts[4] if len(parts) > 4 else "0", 0)
            if target_user_id <= 0 or days <= 0:
                await self.answer_callback_query(callback_id, "Parametr xato", alert=True)
                return
            subscriber = await self.repository.activate_or_extend_bot_subscriber_subscription(
                user_id=target_user_id,
                days=days,
                admin_user_id=user_id,
            )
            if subscriber is None:
                await self.answer_callback_query(callback_id, "Subscriber topilmadi", alert=True)
                return
            with suppress(Exception):
                await self.send_message(subscriber.chat_id, self._subscription_approved_user_text(subscriber, days))
            text, keyboard = await self._build_pending_panel(page=page)
            await self._edit_callback_message(chat_id, message_id, text, keyboard)
            await self.answer_callback_query(callback_id, f"+{days} kun berildi")
            return
        await self.answer_callback_query(callback_id, "Noma'lum admin amal")

    async def _edit_callback_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None,
    ) -> None:
        try:
            await self.edit_message(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
        except Exception as exc:
            if self._is_message_not_modified_error(str(exc)):
                return
            raise

    async def _build_pending_panel(self, page: int = 0) -> tuple[str, dict[str, Any]]:
        subscribers = await self.repository.fetch_pending_bot_subscribers(limit=50)
        page_size = 5
        if not subscribers:
            return ("Pending subscriberlar yo'q.", self._admin_panel_keyboard())
        total = len(subscribers)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(0, page), total_pages - 1)
        start = page * page_size
        end = min(total, start + page_size)
        current = subscribers[start:end]
        lines = [
            f"Pending subscriberlar ({start + 1}-{end}/{total})",
            "Bir tugma bilan tasdiqlash uchun pastdagi tugmalardan foydalaning.",
        ]
        for subscriber in current:
            lines.append(self._subscriber_line(subscriber))
        return ("\n".join(lines), self._pending_panel_keyboard(current, page=page, total_pages=total_pages))

    def _user_panel_text(self) -> str:
        return (
            "Menyu:\n"
            "Tugmalar orqali holatni ko'ring yoki yordamni oching.\n"
            "Obuna yoqish: /start"
        )

    def _admin_panel_text(self) -> str:
        return (
            "Admin panel:\n"
            "Pending, subscriberlar va statistika bo'limlari tugmalar orqali boshqariladi.\n"
            "Paid obuna tasdiqlash uchun Pending bo'limidan foydalaning."
        )

    def _user_panel_keyboard(self, is_admin: bool) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = [
            [
                {"text": "Status", "callback_data": "usr:status"},
                {"text": "Yordam", "callback_data": "usr:help"},
            ]
        ]
        if is_admin:
            rows.append([{"text": "Admin Panel", "callback_data": "adm:menu"}])
        return {"inline_keyboard": rows}

    def _admin_panel_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "Pending", "callback_data": "adm:pending:0"},
                    {"text": "Subscribers", "callback_data": "adm:subs"},
                ],
                [
                    {"text": "Stats", "callback_data": "adm:stats"},
                    {"text": "Check Subs", "callback_data": "adm:check"},
                ],
                [
                    {"text": "User Menu", "callback_data": "usr:menu"},
                ],
            ]
        }

    def _pending_panel_keyboard(
        self,
        subscribers: list[BotSubscriber],
        page: int,
        total_pages: int,
    ) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        default_days = self.subscription_default_days
        for subscriber in subscribers:
            user_label = f"#{str(subscriber.user_id)[-4:]}"
            rows.append(
                [
                    {
                        "text": f"✅ {user_label} +{default_days}d",
                        "callback_data": f"adm:apr:{subscriber.user_id}:{default_days}:{page}",
                    },
                    {
                        "text": "+7d",
                        "callback_data": f"adm:apr:{subscriber.user_id}:7:{page}",
                    },
                    {
                        "text": "+30d",
                        "callback_data": f"adm:apr:{subscriber.user_id}:30:{page}",
                    },
                ]
            )
        nav_row: list[dict[str, str]] = []
        if page > 0:
            nav_row.append({"text": "◀️ Oldingi", "callback_data": f"adm:pending:{page - 1}"})
        nav_row.append({"text": f"{page + 1}/{total_pages}", "callback_data": f"adm:pending:{page}"})
        if page + 1 < total_pages:
            nav_row.append({"text": "Keyingi ▶️", "callback_data": f"adm:pending:{page + 1}"})
        rows.append(nav_row)
        rows.append(
            [
                {"text": "Yangilash", "callback_data": f"adm:pending:{page}"},
                {"text": "Admin Panel", "callback_data": "adm:menu"},
            ]
        )
        return {"inline_keyboard": rows}

    @staticmethod
    def _safe_int(value: str, default: int) -> int:
        try:
            return int(str(value).strip())
        except Exception:
            return default

    @staticmethod
    def _is_message_not_modified_error(error_text: str) -> bool:
        lowered = (error_text or "").lower()
        return "message is not modified" in lowered

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

    async def _build_pending_subscribers_text(self) -> str:
        subscribers = await self.repository.fetch_pending_bot_subscribers(limit=20)
        if not subscribers:
            return "Pending subscriberlar yo'q."
        lines = ["Pending subscriberlar (oxirgi 20 ta):"]
        for subscriber in subscribers:
            lines.append(self._subscriber_line(subscriber))
        return "\n".join(lines)

    def _subscriber_line(self, subscriber: BotSubscriber) -> str:
        username = f"@{subscriber.username}" if subscriber.username else "-"
        status = subscriber.subscription_status or ("active" if subscriber.active else "inactive")
        expires = self._format_expiry_short(subscriber.subscription_expires_at)
        return f"{subscriber.user_id} {username} status={status} expires={expires}"

    def _build_subscriber_status_text(self, subscriber: BotSubscriber | None) -> str:
        if subscriber is None:
            return "Siz hali ro'yxatdan o'tmagansiz. /start bosing."
        active_text = "ha" if subscriber.active else "yo'q"
        lines = [
            "Obuna holati:",
            f"Status: {subscriber.subscription_status}",
            f"Active: {active_text}",
        ]
        if subscriber.subscription_expires_at:
            lines.append(f"Tugash vaqti: {self._format_expiry_human(subscriber.subscription_expires_at)}")
            remaining = self._remaining_hours_text(subscriber.subscription_expires_at)
            if remaining:
                lines.append(f"Qolgan vaqt: {remaining}")
        else:
            lines.append("Tugash vaqti: belgilanmagan")
        if self.paid_subscription_enabled and not self._has_active_access(subscriber):
            lines.append("To'lov tasdiqlangach admin obunangizni faollashtiradi.")
        return "\n".join(lines)

    def _build_admin_extend_result_text(self, subscriber: BotSubscriber, days: int) -> str:
        username = f" @{subscriber.username}" if subscriber.username else ""
        expires = self._format_expiry_human(subscriber.subscription_expires_at)
        return f"Faollashtirildi/uzaytirildi: {subscriber.user_id}{username}\n+{days} kun\nTugash: {expires}"

    def _subscription_approved_user_text(self, subscriber: BotSubscriber, days: int) -> str:
        expires = self._format_expiry_human(subscriber.subscription_expires_at)
        return (
            "Obunangiz tasdiqlandi.\n"
            f"Uzaytirish: +{days} kun\n"
            f"Tugash vaqti: {expires}\n"
            "Status tekshirish: /status"
        )

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
    def _parse_admin_extend_args(arg: str, default_days: int) -> tuple[int | None, int]:
        parts = [p for p in (arg or "").split() if p]
        if not parts:
            return (None, default_days)
        try:
            user_id = int(parts[0])
        except ValueError:
            return (None, default_days)
        days = default_days
        if len(parts) > 1:
            raw_days = parts[1].strip().lower().rstrip("d")
            try:
                days = int(raw_days)
            except ValueError:
                return (None, default_days)
        if user_id <= 0 or days <= 0 or days > 3650:
            return (None, default_days)
        return (user_id, days)

    def _has_active_access(self, subscriber: BotSubscriber | None) -> bool:
        if subscriber is None or not subscriber.active:
            return False
        if not self.paid_subscription_enabled:
            return True
        if subscriber.subscription_status != "active":
            return False
        expires = self._parse_datetime(subscriber.subscription_expires_at)
        if expires is None:
            return False
        return expires > datetime.now(UTC)

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @classmethod
    def _format_expiry_short(cls, value: str | None) -> str:
        dt = cls._parse_datetime(value)
        if dt is None:
            return "-"
        return dt.strftime("%Y-%m-%d")

    @classmethod
    def _format_expiry_human(cls, value: str | None) -> str:
        dt = cls._parse_datetime(value)
        if dt is None:
            return "belgilanmagan"
        return dt.strftime("%Y-%m-%d %H:%M UTC")

    @classmethod
    def _remaining_hours_text(cls, value: str | None) -> str:
        dt = cls._parse_datetime(value)
        if dt is None:
            return ""
        remaining_sec = int((dt - datetime.now(UTC)).total_seconds())
        if remaining_sec <= 0:
            return "muddat tugagan"
        hours = remaining_sec // 3600
        days = hours // 24
        rem_hours = hours % 24
        if days > 0:
            return f"{days} kun {rem_hours} soat"
        return f"{max(1, hours)} soat"

    @staticmethod
    def _welcome_text() -> str:
        return (
            "Obuna muvaffaqiyatli yoqildi.\n"
            "Buyruqlar:\n"
            "/start - obunani yoqish\n"
            "/stop - obunani to'xtatish\n"
            "/status - holatni ko'rish\n"
            "/help - yordam"
        )

    @staticmethod
    def _welcome_pending_text(default_days: int) -> str:
        return (
            "So'rovingiz qabul qilindi.\n"
            "Paid obuna yoqilgan: admin to'lovni tasdiqlagach guruhga ruxsat beriladi.\n"
            f"Standart muddat: {default_days} kun.\n"
            "Holat: /status"
        )

    def _help_text(self) -> str:
        admin_lines = [
            "/stats (admin)",
            "/subscribers (admin)",
            "/pending (admin)",
            f"/approve (user_id) [kun] (admin, default={self.subscription_default_days})",
            "/extend (user_id) (kun) (admin)",
            "/checksubs (admin)",
            "/broadcast (text) (admin, BOT_BROADCAST_SUBSCRIBERS=true bo'lsa ishlaydi)",
        ]
        return (
            "Buyruqlar:\n"
            "/start\n"
            "/stop\n"
            "/status\n"
            "/help\n"
            + "\n".join(admin_lines)
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

    async def _maybe_run_subscription_maintenance(self) -> None:
        if not self.paid_subscription_enabled:
            return
        now_mono = time.monotonic()
        if (
            self._last_subscription_maintenance_monotonic > 0
            and now_mono - self._last_subscription_maintenance_monotonic < self.subscription_check_interval_sec
        ):
            return
        self._last_subscription_maintenance_monotonic = now_mono
        await self._run_subscription_maintenance()

    async def _run_subscription_maintenance(self) -> None:
        if not self.paid_subscription_enabled:
            return
        expired = await self.repository.expire_due_bot_subscribers(limit=200)
        for subscriber in expired:
            with suppress(Exception):
                await self.send_message(subscriber.chat_id, self._subscription_expired_text())
            if self.remove_expired_from_groups and self.managed_private_group_ids:
                await self._remove_user_from_managed_groups(subscriber.user_id)
        reminders = await self.repository.fetch_expiring_bot_subscribers(
            reminder_hours=self.subscription_reminder_hours,
            limit=200,
        )
        for subscriber in reminders:
            try:
                await self.send_message(subscriber.chat_id, self._subscription_expiring_text(subscriber))
                await self.repository.mark_bot_subscriber_reminder_sent(subscriber.user_id)
            except Exception:
                logger.exception(
                    "subscription_reminder_send_failed",
                    extra={"chat_id": subscriber.chat_id, "user_id": subscriber.user_id},
                )
        if expired or reminders:
            logger.info(
                "subscription_maintenance_done",
                extra={
                    "action": "subscription_maintenance",
                    "count": len(reminders),
                    "reason": f"expired={len(expired)}",
                },
            )

    async def _handle_chat_join_request(self, payload: dict[str, Any]) -> None:
        chat = payload.get("chat") or {}
        user = payload.get("from") or {}
        chat_id = int(chat.get("id") or 0)
        user_id = int(user.get("id") or 0)
        if chat_id == 0 or user_id <= 0:
            return
        if self.managed_private_group_ids and chat_id not in self.managed_private_group_ids:
            return

        username = user.get("username")
        first_name = user.get("first_name")
        user_chat_id = int(payload.get("user_chat_id") or user_id)
        existing = await self.repository.fetch_bot_subscriber_by_user_id(user_id)
        upsert_active = existing.active if existing is not None else (not self.paid_subscription_enabled)
        with suppress(Exception):
            await self.repository.upsert_bot_subscriber(
                user_id=user_id,
                chat_id=user_chat_id,
                username=username,
                first_name=first_name,
                active=upsert_active,
            )

        if not self.paid_subscription_enabled:
            if self.auto_approve_join_requests:
                await self._approve_chat_join_request(chat_id, user_id)
            return

        subscriber = await self.repository.fetch_bot_subscriber_by_user_id(user_id)
        if self._has_active_access(subscriber):
            if self.auto_approve_join_requests:
                await self._approve_chat_join_request(chat_id, user_id)
            with suppress(Exception):
                await self.send_message(user_chat_id, "Join request tasdiqlandi. Xush kelibsiz.")
            return

        # Paid obuna yoqilgan va userda aktiv muddat yo'q.
        with suppress(Exception):
            await self.repository.mark_bot_subscriber_pending(user_id)
        if self.decline_unpaid_join_requests:
            with suppress(Exception):
                await self._decline_chat_join_request(chat_id, user_id)
        with suppress(Exception):
            await self.send_message(user_chat_id, self._join_request_rejected_text())

    async def _approve_chat_join_request(self, chat_id: int, user_id: int) -> None:
        await self._api_call("approveChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})

    async def _decline_chat_join_request(self, chat_id: int, user_id: int) -> None:
        await self._api_call("declineChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})

    async def _remove_user_from_managed_groups(self, user_id: int) -> None:
        for group_id in self.managed_private_group_ids:
            try:
                await self._api_call(
                    "banChatMember",
                    {
                        "chat_id": group_id,
                        "user_id": user_id,
                        "revoke_messages": False,
                    },
                )
                await self._api_call(
                    "unbanChatMember",
                    {
                        "chat_id": group_id,
                        "user_id": user_id,
                        "only_if_banned": True,
                    },
                )
            except Exception:
                logger.exception(
                    "managed_group_remove_failed",
                    extra={"action": "group_remove", "chat_id": group_id, "user_id": user_id},
                )
            await asyncio.sleep(0.05)

    def _subscription_expiring_text(self, subscriber: BotSubscriber) -> str:
        remaining = self._remaining_hours_text(subscriber.subscription_expires_at)
        expires = self._format_expiry_human(subscriber.subscription_expires_at)
        return (
            "Obuna muddati tugashiga oz qoldi.\n"
            f"Tugash vaqti: {expires}\n"
            f"Qolgan vaqt: {remaining}\n"
            "To'lovni yangilash uchun admin bilan bog'laning."
        )

    @staticmethod
    def _subscription_expired_text() -> str:
        return (
            "Obuna muddati tugadi.\n"
            "Private guruhga kirish cheklanadi.\n"
            "Qayta faollashtirish uchun admin bilan bog'laning yoki /start yuboring."
        )

    @staticmethod
    def _join_request_rejected_text() -> str:
        return (
            "Join request qabul qilinmadi.\n"
            "Sabab: aktiv paid obuna topilmadi.\n"
            "Avval /start yuboring va to'lov tasdiqlanishini kuting."
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
