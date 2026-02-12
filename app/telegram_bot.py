from __future__ import annotations

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
        self.workers = WorkerPool(
            queue=queue,
            processor=self._process_message,
            worker_count=settings.worker_count,
            poll_timeout=settings.worker_poll_timeout,
        )
        self._wire_handlers()

    def _wire_handlers(self) -> None:
        async def process_event(event: events.common.EventCommon) -> None:
            if event.chat_id is None:
                return

            raw_text = self._extract_text(event)
            if raw_text:
                await self._discover_private_invites(raw_text, int(event.chat_id))

            if event.is_private:
                return
            if not raw_text:
                logger.info(
                    "message_filtered",
                    extra={"chat_id": event.chat_id, "message_id": getattr(event, "id", 0), "reason": "no_text"},
                )
                return

            normalized = normalize_text(raw_text)
            if not normalized:
                logger.info(
                    "message_filtered",
                    extra={"chat_id": event.chat_id, "message_id": getattr(event, "id", 0), "reason": "empty"},
                )
                return

            result = self.filter_engine.evaluate(normalized)
            if not result.passed:
                logger.info(
                    "message_filtered",
                    extra={
                        "chat_id": event.chat_id,
                        "message_id": getattr(event, "id", 0),
                        "reason": result.reason,
                    },
                )
                return

            envelope = MessageEnvelope(
                chat_id=int(event.chat_id),
                message_id=event.id,
                sender_id=event.sender_id,
                raw_text=raw_text,
                chat_username=getattr(getattr(event, "chat", None), "username", None),
                chat_title=getattr(getattr(event, "chat", None), "title", None),
            )
            await self.queue.put(NormalizedMessage(envelope=envelope, normalized_text=normalized))

        @self.client.on(events.NewMessage(incoming=True))
        async def on_new_message(event: events.NewMessage.Event) -> None:
            await process_event(event)

        @self.client.on(events.MessageEdited(incoming=True))
        async def on_message_edited(event: events.MessageEdited.Event) -> None:
            await process_event(event)

        @self.client.on(events.NewMessage(pattern=r"^/(kw|keyword)\b"))
        async def on_keyword_command(event: events.NewMessage.Event) -> None:
            await self._handle_keyword_command(event)

    async def start(self) -> None:
        await self.workers.start()
        await self.client.start()
        if self._owner_user_id is None:
            me = await self.client.get_me()
            self._owner_user_id = int(me.id)
        logger.info("userbot_started")
        await self.client.run_until_disconnected()

    async def shutdown(self) -> None:
        with suppress(Exception):
            await self.workers.stop()
        with suppress(Exception):
            await self.client.disconnect()

    async def _process_message(self, msg: NormalizedMessage) -> None:
        decision = self.decision_engine.decide(msg)
        if not decision.should_forward:
            logger.info(
                "decision_skip",
                extra={
                    "chat_id": msg.envelope.chat_id,
                    "message_id": msg.envelope.message_id,
                    "decision": "skip",
                    "reason": decision.reason,
                },
            )
            return
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
