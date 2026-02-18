from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress

from telethon import TelegramClient, events

from app.actions import ActionExecutor
from app.config import Settings
from app.filtering import FastFilter
from app.keywords import KeywordService
from app.message_queue import MessageQueue
from app.models import MessageEnvelope, NormalizedMessage
from app.runtime_config import RuntimeConfigService
from app.rules import DecisionEngine, RuleConfig
from app.storage.db import ActionRepository, KEYWORD_KINDS
from app.text import normalize_text
from app.workers import WorkerPool

logger = logging.getLogger(__name__)

_PRIVATE_INVITE_RE = re.compile(r"https?://t\.me/(?:\+|joinchat/)[A-Za-z0-9_-]+")


class TelegramUserbot:
    def __init__(
        self,
        settings: Settings,
        client: TelegramClient,
        queue: MessageQueue,
        filter_engine: FastFilter,
        decision_engine: DecisionEngine,
        executor: ActionExecutor,
        repository: ActionRepository,
        keyword_service: KeywordService,
    ) -> None:
        self.settings = settings
        self.client = client
        self.queue = queue
        self.filter_engine = filter_engine
        self.decision_engine = decision_engine
        self.executor = executor
        self.repository = repository
        self.keyword_service = keyword_service
        self._owner_user_id = settings.owner_user_id
        self._chat_last_seen: dict[int, int] = {}
        self._dirty_chat_states: set[int] = set()
        self._history_task: asyncio.Task[None] | None = None
        self._history_stop = asyncio.Event()
        self.workers = WorkerPool(
            queue=queue,
            processor=self._process_message,
            worker_count=settings.worker_count,
            poll_timeout=settings.worker_poll_timeout,
        )
        self._wire_handlers()

    def _wire_handlers(self) -> None:
        async def process_event(event: events.common.EventCommon, source: str) -> None:
            if event.chat_id is None:
                return

            raw_text = self._extract_text(event)
            if raw_text:
                await self._discover_private_invites(raw_text, int(event.chat_id))
            if event.is_private:
                return
            await self._ingest_message(
                chat_id=int(event.chat_id),
                message_id=int(getattr(event, "id", 0)),
                sender_id=event.sender_id,
                raw_text=raw_text,
                chat_username=getattr(getattr(event, "chat", None), "username", None),
                chat_title=getattr(getattr(event, "chat", None), "title", None),
                source=source,
            )

        @self.client.on(events.NewMessage(incoming=True))
        async def on_new_message(event: events.NewMessage.Event) -> None:
            await process_event(event, source="realtime")

        if not self.settings.realtime_only:
            @self.client.on(events.MessageEdited(incoming=True))
            async def on_message_edited(event: events.MessageEdited.Event) -> None:
                await process_event(event, source="edited")

        @self.client.on(events.NewMessage(pattern=r"^/(kw|keyword)\b"))
        async def on_keyword_command(event: events.NewMessage.Event) -> None:
            await self._handle_keyword_command(event)

    async def start(self) -> None:
        await self.workers.start()
        await self.client.start()
        if self._owner_user_id is None:
            me = await self.client.get_me()
            self._owner_user_id = int(me.id)
        await self._load_chat_read_states()
        history_enabled = self.settings.history_sync_enabled and not self.settings.realtime_only
        if history_enabled:
            await self._history_sync_once(source="startup")
            await self._flush_chat_read_states()
            self._history_task = asyncio.create_task(self._history_sync_loop(), name="history-sync")
        else:
            logger.info(
                "history_sync_disabled",
                extra={
                    "action": "history_sync",
                    "reason": "realtime_only" if self.settings.realtime_only else "disabled",
                },
            )
        logger.info("userbot_started")
        await self.client.run_until_disconnected()

    async def shutdown(self) -> None:
        self._history_stop.set()
        if self._history_task:
            self._history_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._history_task
        with suppress(Exception):
            await self._flush_chat_read_states()
        with suppress(Exception):
            await self.workers.stop()
        with suppress(Exception):
            await self.client.disconnect()

    async def _ingest_message(
        self,
        chat_id: int,
        message_id: int,
        sender_id: int | None,
        raw_text: str,
        chat_username: str | None,
        chat_title: str | None,
        source: str,
    ) -> None:
        if message_id <= 0:
            return

        base_context = self._build_message_log_context(
            chat_id=chat_id,
            chat_username=chat_username,
            chat_title=chat_title,
            raw_text=raw_text,
        )
        self._mark_chat_seen(chat_id, message_id)
        logger.info(
            "message_received",
            extra={
                "action": "message_receive",
                "source": source,
                "chat_id": chat_id,
                "message_id": message_id,
                **base_context,
            },
        )

        if not raw_text:
            logger.info(
                "message_filtered",
                extra={
                    "action": "filter_drop",
                    "source": source,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reason": "no_text",
                    **base_context,
                },
            )
            return

        normalized = normalize_text(raw_text)
        normalized_context = self._build_message_log_context(
            chat_id=chat_id,
            chat_username=chat_username,
            chat_title=chat_title,
            raw_text=raw_text,
            normalized_text=normalized,
        )
        if not normalized:
            logger.info(
                "message_filtered",
                extra={
                    "action": "filter_drop",
                    "source": source,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reason": "empty",
                    **normalized_context,
                },
            )
            return

        result = self.filter_engine.evaluate(normalized)
        if not result.passed:
            logger.info(
                "message_filtered",
                extra={
                    "action": "filter_drop",
                    "source": source,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reason": result.reason,
                    **normalized_context,
                },
            )
            return

        envelope = MessageEnvelope(
            chat_id=chat_id,
            message_id=message_id,
            sender_id=sender_id,
            raw_text=raw_text,
            chat_username=chat_username,
            chat_title=chat_title,
        )
        await self.queue.put(NormalizedMessage(envelope=envelope, normalized_text=normalized))
        logger.info(
            "message_queued",
            extra={
                "action": "queue_put",
                "source": source,
                "chat_id": chat_id,
                "message_id": message_id,
                "queue_size": self.queue.qsize(),
                **normalized_context,
            },
        )

    async def _history_sync_loop(self) -> None:
        while not self._history_stop.is_set():
            if not self.client.is_connected():
                await asyncio.sleep(5)
                continue
            if not await self.client.is_user_authorized():
                await asyncio.sleep(5)
                continue
            try:
                await self._history_sync_once(source="history")
                await self._flush_chat_read_states()
            except Exception:
                logger.exception("history_sync_loop_failed")
            await asyncio.sleep(self.settings.history_sync_interval_sec)

    async def _history_sync_once(self, source: str) -> None:
        logger.info("history_sync_started", extra={"action": "history_sync", "source": source})
        scanned_chats = 0
        scanned_messages = 0
        for dialog in await self.client.get_dialogs():
            if not (dialog.is_group or dialog.is_channel):
                continue
            scanned_chats += 1
            try:
                processed = await self._scan_dialog_history(dialog, source=source)
                scanned_messages += processed
            except Exception:
                logger.exception(
                    "history_chat_scan_failed",
                    extra={"action": "history_chat_scan", "chat_id": int(getattr(dialog, "id", 0))},
                )

        logger.info(
            "history_sync_completed",
            extra={
                "action": "history_sync_done",
                "source": source,
                "count": scanned_messages,
                "reason": f"chats={scanned_chats}",
            },
        )

    async def _scan_dialog_history(self, dialog: object, source: str) -> int:
        chat_id = int(getattr(dialog, "id"))
        chat_title = getattr(dialog, "name", "") or ""
        entity = getattr(dialog, "entity")
        chat_username = getattr(entity, "username", None)
        last_seen = self._chat_last_seen.get(chat_id, 0)
        if last_seen <= 0:
            latest_message_id = self._dialog_latest_message_id(dialog)
            if latest_message_id > 0:
                self._mark_chat_seen(chat_id, latest_message_id)
                logger.info(
                    "history_chat_baselined",
                    extra={
                        "action": "history_chat_scan",
                        "source": source,
                        "chat_id": chat_id,
                        "count": 0,
                        "reason": f"skip_old_messages baseline={latest_message_id}",
                    },
                )
                return 0

        scanned = 0
        max_seen = last_seen
        async for message in self.client.iter_messages(
            entity=entity,
            min_id=last_seen,
            reverse=True,
            limit=self.settings.history_sync_batch_size,
        ):
            message_id = int(getattr(message, "id", 0) or 0)
            if message_id <= 0:
                continue
            if message_id > max_seen:
                max_seen = message_id
            if bool(getattr(message, "out", False)):
                continue

            raw_text = self._extract_text_from_message(message)
            if raw_text:
                await self._discover_private_invites(raw_text, chat_id)
            await self._ingest_message(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=getattr(message, "sender_id", None),
                raw_text=raw_text,
                chat_username=chat_username,
                chat_title=chat_title,
                source=source,
            )
            scanned += 1

        if max_seen > last_seen:
            self._mark_chat_seen(chat_id, max_seen)

        logger.info(
            "history_chat_scanned",
            extra={
                "action": "history_chat_scan",
                "source": source,
                "chat_id": chat_id,
                "count": scanned,
                "reason": f"last_seen={max_seen}",
            },
        )
        return scanned

    @staticmethod
    def _dialog_latest_message_id(dialog: object) -> int:
        latest_message = getattr(dialog, "message", None)
        latest_id = int(getattr(latest_message, "id", 0) or 0)
        if latest_id > 0:
            return latest_id
        return int(getattr(dialog, "top_message", 0) or 0)

    async def _load_chat_read_states(self) -> None:
        try:
            self._chat_last_seen = await self.repository.fetch_chat_read_states()
            logger.info(
                "chat_read_state_loaded",
                extra={"action": "state_load", "count": len(self._chat_last_seen)},
            )
        except Exception:
            logger.exception("chat_read_state_load_failed")
            self._chat_last_seen = {}

    def _mark_chat_seen(self, chat_id: int, message_id: int) -> None:
        previous = self._chat_last_seen.get(chat_id, 0)
        if message_id > previous:
            self._chat_last_seen[chat_id] = message_id
            self._dirty_chat_states.add(chat_id)

    async def _flush_chat_read_states(self) -> None:
        if not self._dirty_chat_states:
            return
        flushed = 0
        for chat_id in list(self._dirty_chat_states):
            last_seen = self._chat_last_seen.get(chat_id, 0)
            if last_seen <= 0:
                self._dirty_chat_states.discard(chat_id)
                continue
            try:
                await self.repository.upsert_chat_read_state(chat_id, last_seen)
                self._dirty_chat_states.discard(chat_id)
                flushed += 1
            except Exception:
                logger.exception("chat_read_state_flush_failed", extra={"chat_id": chat_id})
        if flushed:
            logger.info("chat_read_state_flushed", extra={"action": "state_flush", "count": flushed})

    async def _process_message(self, msg: NormalizedMessage) -> None:
        decision = self.decision_engine.decide(msg)
        decision_context = self._build_message_log_context(
            chat_id=msg.envelope.chat_id,
            chat_username=msg.envelope.chat_username,
            chat_title=msg.envelope.chat_title,
            raw_text=msg.envelope.raw_text,
            normalized_text=msg.normalized_text,
        )
        if not decision.should_forward:
            logger.info(
                "decision_skip",
                extra={
                    "chat_id": msg.envelope.chat_id,
                    "message_id": msg.envelope.message_id,
                    "decision": "skip",
                    "reason": decision.reason,
                    **decision_context,
                },
            )
            return
        logger.info(
            "decision_forward",
            extra={
                "chat_id": msg.envelope.chat_id,
                "message_id": msg.envelope.message_id,
                "decision": "forward",
                "reason": decision.reason,
                **decision_context,
            },
        )
        await self.executor.execute(msg, decision)

    async def _discover_private_invites(self, raw_text: str, source_chat_id: int) -> None:
        for match in _PRIVATE_INVITE_RE.findall(raw_text):
            try:
                await self.repository.upsert_private_invite_link(match, source_chat_id=source_chat_id)
            except Exception:
                logger.exception("private_invite_store_failed")

    async def _handle_keyword_command(self, event: events.NewMessage.Event) -> None:
        if not event.is_private:
            return
        if self._owner_user_id is None:
            return
        if int(event.sender_id or 0) != self._owner_user_id:
            return

        command = (event.raw_text or "").strip()
        parts = command.split(maxsplit=3)
        if len(parts) < 2:
            await event.reply(self._command_help())
            return

        action = parts[1].lower()
        if action == "list":
            keywords = await self.keyword_service.list_keywords()
            response = "\n".join(
                f"{kind}: {', '.join(values[:20])}" for kind, values in keywords.items()
            )
            await event.reply(response or "No keywords")
            return

        if action == "reload":
            await self.keyword_service.reload()
            await event.reply("Keywordlar yangilandi.")
            return

        if len(parts) < 4:
            await event.reply(self._command_help())
            return

        kind = parts[2].strip().lower()
        value = parts[3].strip()
        if kind not in KEYWORD_KINDS:
            await event.reply(f"Kind noto'g'ri. Ruxsat: {', '.join(KEYWORD_KINDS)}")
            return

        try:
            if action == "add":
                added = await self.keyword_service.add_keyword(kind, value)
                await event.reply(f"Qo'shildi: {', '.join(added) if added else 'none'}")
                return
            if action in {"del", "delete", "remove"}:
                deleted = await self.keyword_service.delete_keyword(kind, value)
                await event.reply(f"O'chirildi: {', '.join(deleted) if deleted else 'none'}")
                return
        except Exception:
            logger.exception("keyword_command_failed")
            await event.reply("Xatolik bo'ldi.")
            return

        await event.reply(self._command_help())

    @staticmethod
    def _command_help() -> str:
        return (
            "Keyword buyruqlari:\n"
            "/kw list\n"
            "/kw reload\n"
            "/kw add <kind> <value>\n"
            "/kw del <kind> <value>"
        )

    @staticmethod
    def _extract_text(event: events.common.EventCommon) -> str:
        raw = getattr(event, "raw_text", None)
        if raw:
            return raw

        message = getattr(event, "message", None)
        if not message:
            return ""
        if message.message:
            return message.message

        # Some sticker/document messages only carry a file emoji or name.
        file_obj = getattr(message, "file", None)
        if file_obj is not None:
            file_emoji = getattr(file_obj, "emoji", None)
            if file_emoji:
                return file_emoji
            file_name = getattr(file_obj, "name", None)
            if file_name:
                return file_name
        return ""

    @staticmethod
    def _extract_text_from_message(message: object) -> str:
        text = getattr(message, "message", None)
        if text:
            return str(text)
        file_obj = getattr(message, "file", None)
        if file_obj is not None:
            file_emoji = getattr(file_obj, "emoji", None)
            if file_emoji:
                return str(file_emoji)
            file_name = getattr(file_obj, "name", None)
            if file_name:
                return str(file_name)
        return ""

    @classmethod
    def _build_message_log_context(
        cls,
        chat_id: int,
        chat_username: str | None,
        chat_title: str | None,
        raw_text: str,
        normalized_text: str | None = None,
    ) -> dict[str, object]:
        username = (chat_username or "").strip().lstrip("@")
        title = (chat_title or "").strip()
        chat_ref = f"@{username}" if username else (title or str(chat_id))

        context: dict[str, object] = {
            "chat_ref": chat_ref,
            "raw_preview": cls._preview_text(raw_text),
        }
        if username:
            context["chat_username"] = f"@{username}"
        if title:
            context["chat_title"] = title
        if normalized_text is not None:
            context["normalized_preview"] = cls._preview_text(normalized_text)
        return context

    @staticmethod
    def _preview_text(text: str, limit: int = 180) -> str:
        compact = " ".join((text or "").split())
        if len(compact) <= limit:
            return compact
        return f"{compact[: max(0, limit - 3)]}..."


def build_userbot(
    settings: Settings,
    client: TelegramClient,
    queue: MessageQueue,
    executor: ActionExecutor,
    repository: ActionRepository,
    keyword_service: KeywordService,
    runtime_config: RuntimeConfigService | None = None,
) -> TelegramUserbot:
    return TelegramUserbot(
        settings=settings,
        client=client,
        queue=queue,
        filter_engine=FastFilter(
            min_length=settings.min_text_length,
            keyword_service=keyword_service,
            runtime_config=runtime_config,
        ),
        decision_engine=DecisionEngine(
            RuleConfig(min_length=settings.min_text_length),
            keyword_service=keyword_service,
            runtime_config=runtime_config,
        ),
        executor=executor,
        repository=repository,
        keyword_service=keyword_service,
    )
