from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Protocol

from telethon import TelegramClient, functions

from app.config import Settings
from app.models import Decision, NormalizedMessage
from app.rate_limit import CooldownManager
from app.runtime_config import RuntimeConfigService
from app.storage.db import ActionRepository

logger = logging.getLogger(__name__)


class BotPublisher(Protocol):
    async def send_message(self, chat_id: str | int, text: str) -> int:
        ...

    async def edit_message(self, chat_id: str | int, message_id: int, text: str) -> None:
        ...

    async def broadcast_to_subscribers(self, text: str) -> tuple[int, int]:
        ...


class ActionExecutor:
    def __init__(
        self,
        client: TelegramClient,
        settings: Settings,
        cooldown: CooldownManager,
        repository: ActionRepository,
        runtime_config: RuntimeConfigService | None = None,
        bot_publisher: BotPublisher | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.cooldown = cooldown
        self.repository = repository
        self.runtime_config = runtime_config
        self.bot_publisher = bot_publisher
        self._published_order_map: dict[tuple[int, int], tuple[str | int, int]] = {}

    async def execute(self, msg: NormalizedMessage, decision: Decision) -> None:
        if not decision.should_forward:
            return

        runtime = self.runtime_config.snapshot() if self.runtime_config else None
        per_group_actions_hour = (
            runtime.per_group_actions_hour if runtime else self.settings.per_group_actions_hour
        )
        global_actions_minute = (
            runtime.global_actions_minute if runtime else self.settings.global_actions_minute
        )
        forward_target = runtime.forward_target if runtime else self.settings.forward_target

        chat_id = msg.envelope.chat_id
        allow_chat = await self.cooldown.allow_action(
            chat_id,
            "any",
            per_group_actions_hour,
            3600,
        )
        allow_global = await self.cooldown.allow_global(
            "any",
            global_actions_minute,
            60,
        )
        if not (allow_chat and allow_global):
            logger.info(
                "action_blocked_rate_limit",
                extra={"chat_id": chat_id, "message_id": msg.envelope.message_id, "decision": "blocked"},
            )
            return

        await self._human_pause()
        source_link = self._build_source_link(msg)
        publish_key = (msg.envelope.chat_id, msg.envelope.message_id)
        existing_publish = self._published_order_map.get(publish_key)
        status_label = "Yangilandi" if existing_publish else "Yangi"
        outbound = self.format_publish_message(
            raw_text=msg.envelope.raw_text,
            source_link=source_link,
            region_tag=decision.region_tag,
            status_label=status_label,
        )
        if existing_publish:
            target_entity, target_message_id = existing_publish
            try:
                logger.info(
                    "publish_edit_attempt",
                    extra={
                        "action": "publish_edit_attempt",
                        "chat_id": chat_id,
                        "message_id": msg.envelope.message_id,
                        "target": str(target_entity),
                        "target_message_id": target_message_id,
                    },
                )
                if self.bot_publisher:
                    await self.bot_publisher.edit_message(
                        chat_id=target_entity,
                        message_id=target_message_id,
                        text=outbound,
                    )
                else:
                    await self.client.edit_message(
                        entity=target_entity,
                        message=target_message_id,
                        text=outbound,
                        link_preview=False,
                    )
                logger.info(
                    "publish_edit_ok",
                    extra={
                        "action": "publish_edit",
                        "chat_id": chat_id,
                        "message_id": msg.envelope.message_id,
                        "target": str(target_entity),
                        "target_message_id": target_message_id,
                        "status": "ok",
                    },
                )
                await self.repository.insert_action(chat_id, msg.envelope.message_id, "publish_edit", "ok")
                return
            except Exception:
                logger.exception(
                    "publish_edit_failed",
                    extra={
                        "chat_id": chat_id,
                        "message_id": msg.envelope.message_id,
                        "target": str(target_entity),
                        "target_message_id": target_message_id,
                    },
                )
                await self.repository.insert_action(chat_id, msg.envelope.message_id, "publish_edit", "error")
                self._published_order_map.pop(publish_key, None)

        target_entity = self._resolve_forward_target(forward_target)
        logger.info(
            "publish_attempt",
            extra={
                "action": "publish_attempt",
                "chat_id": chat_id,
                "message_id": msg.envelope.message_id,
                "target": str(target_entity),
            },
        )
        sent_message_id = 0
        if self.bot_publisher:
            sent_message_id = await self.bot_publisher.send_message(chat_id=target_entity, text=outbound)
        else:
            sent_message = await self.client.send_message(
                entity=target_entity,
                message=outbound,
                link_preview=False,
            )
            sent_message_id = int(getattr(sent_message, "id", 0) or 0)
        if sent_message_id > 0:
            if len(self._published_order_map) >= 10_000:
                self._published_order_map.pop(next(iter(self._published_order_map)))
            self._published_order_map[publish_key] = (target_entity, sent_message_id)
        logger.info(
            "publish_ok",
            extra={
                "action": "publish",
                "chat_id": chat_id,
                "message_id": msg.envelope.message_id,
                "target": str(target_entity),
                "status": "ok",
            },
        )
        await self.repository.insert_action(chat_id, msg.envelope.message_id, "publish", "ok")
        if self.bot_publisher:
            sent_count, failed_count = await self.bot_publisher.broadcast_to_subscribers(outbound)
            if sent_count or failed_count:
                logger.info(
                    "subscriber_broadcast_done",
                    extra={
                        "action": "bot_broadcast",
                        "count": sent_count,
                        "reason": f"failed={failed_count}",
                    },
                )

    async def try_join(self, invite_link: str) -> bool:
        runtime = self.runtime_config.snapshot() if self.runtime_config else None
        join_limit_day = runtime.join_limit_day if runtime else self.settings.join_limit_day
        if not await self.cooldown.allow_join(join_limit_day):
            logger.info("join_blocked_limit", extra={"action": "join", "reason": "join_limit"})
            return False
        try:
            await self._human_pause()
            invite_hash = invite_link.rsplit("/", 1)[-1].lstrip("+")
            logger.info("join_attempt", extra={"action": "join_attempt", "reason": invite_link[:120]})
            await self.client(functions.messages.ImportChatInviteRequest(invite_hash))
            logger.info("join_ok", extra={"action": "join", "status": "ok"})
            await self.repository.insert_action(0, 0, "join", "ok")
            return True
        except Exception:
            logger.exception("join_failed")
            await self.repository.insert_action(0, 0, "join", "error")
            return False

    async def try_join_public(self, username: str, peer_id: int) -> bool:
        if not username:
            return False
        runtime = self.runtime_config.snapshot() if self.runtime_config else None
        join_limit_day = runtime.join_limit_day if runtime else self.settings.join_limit_day
        if not await self.cooldown.allow_join(join_limit_day):
            logger.info("join_public_blocked_limit", extra={"action": "join_public", "chat_id": peer_id})
            return False
        try:
            await self._human_pause()
            logger.info(
                "join_public_attempt",
                extra={"action": "join_public_attempt", "chat_id": peer_id, "reason": username},
            )
            await self.client(functions.channels.JoinChannelRequest(channel=username))
            logger.info("join_public_ok", extra={"action": "join_public", "chat_id": peer_id, "status": "ok"})
            await self.repository.insert_action(peer_id, 0, "join_public", "ok")
            return True
        except Exception:
            logger.exception("join_public_failed", extra={"chat_id": peer_id})
            await self.repository.insert_action(peer_id, 0, "join_public", "error")
            return False

    async def _simulate_typing(self, chat_id: int) -> None:
        delay_min, delay_max = self._delay_bounds()
        duration = random.uniform(delay_min, delay_max)
        async with self.client.action(chat_id, "typing"):
            await asyncio.sleep(duration)

    async def _human_pause(self) -> None:
        delay_min, delay_max = self._delay_bounds()
        await asyncio.sleep(random.uniform(delay_min, delay_max))

    def _delay_bounds(self) -> tuple[float, float]:
        runtime = self.runtime_config.snapshot() if self.runtime_config else None
        if runtime:
            return runtime.min_human_delay_sec, runtime.max_human_delay_sec
        return self.settings.min_human_delay_sec, self.settings.max_human_delay_sec

    @staticmethod
    def _resolve_forward_target(target: str | int) -> str | int:
        if isinstance(target, int):
            return target
        value = str(target).strip()
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        return value

    def _build_source_link(self, msg: NormalizedMessage) -> str:
        username = (msg.envelope.chat_username or "").strip().lstrip("@")
        if username:
            return f"https://t.me/{username}/{msg.envelope.message_id}"

        abs_id = abs(msg.envelope.chat_id)
        abs_text = str(abs_id)
        if abs_text.startswith("100") and len(abs_text) > 3:
            return f"https://t.me/c/{abs_text[3:]}/{msg.envelope.message_id}"
        return ""

    @staticmethod
    def format_publish_message(
        raw_text: str,
        source_link: str,
        region_tag: str | None,
        status_label: str = "Yangi",
    ) -> str:
        region = region_tag or "#Uzbekiston"
        body = (raw_text or "").strip() or "(matn topilmadi)"
        lines = [
            "Taxi buyurtma:",
            body,
            region,
            f"Status: {status_label}",
        ]
        if source_link:
            lines.append(f"Manba: {source_link}")
        else:
            lines.append("Manba: private chat")

        message = "\n\n".join(lines)
        if len(message) <= 3900:
            return message

        # Keep room for region + status + source lines inside Telegram limits.
        tail = f"\n\n{region}\n\nStatus: {status_label}\n\nManba: {source_link or 'private chat'}"
        head_limit = max(120, 3900 - len(tail) - 24)
        compact_body = (body[:head_limit] + "...") if len(body) > head_limit else body
        return f"Taxi buyurtma:\n\n{compact_body}{tail}"
