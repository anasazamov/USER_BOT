from __future__ import annotations

import asyncio
import html
import logging
import random
import re
from typing import Protocol

from telethon import TelegramClient, functions, utils

from app.config import Settings
from app.models import Decision, NormalizedMessage
from app.rate_limit import CooldownManager
from app.runtime_config import RuntimeConfigService
from app.storage.db import ActionRepository

logger = logging.getLogger(__name__)

_TME_CHAT_REF_RE = re.compile(
    r"^(?:https?://)?t\.me/(?:(?:c/(?P<c_id>\d+)(?:/\d+)?)|(?P<username>[A-Za-z0-9_]+)(?:/\d+)?)/?$",
    re.IGNORECASE,
)
_PRIVATE_INVITE_REF_RE = re.compile(
    r"^(?:https?://)?t\.me/(?:(?:\+)|(?:joinchat/))(?P<invite>[A-Za-z0-9_-]{8,128})/?$",
    re.IGNORECASE,
)


class BotPublisher(Protocol):
    async def send_message(self, chat_id: str | int, text: str) -> int:
        ...

    async def edit_message(self, chat_id: str | int, message_id: int, text: str) -> None:
        ...

    async def send_message_with_entities(
        self,
        chat_id: str | int,
        text: str,
        entities: list[dict[str, object]],
    ) -> int:
        ...

    async def edit_message_with_entities(
        self,
        chat_id: str | int,
        message_id: int,
        text: str,
        entities: list[dict[str, object]],
    ) -> None:
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
        self._private_invite_route_map: dict[str, int] = {}

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

        chat_id = msg.envelope.chat_id
        allow_chat = True
        if per_group_actions_hour > 0:
            allow_chat = await self.cooldown.allow_action(
                chat_id,
                "any",
                per_group_actions_hour,
                3600,
            )
        allow_global = True
        if global_actions_minute > 0:
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

        if not self.bot_publisher:
            await self._human_pause()
        source_link = self._build_source_link(msg)
        sender_profile_link = self._build_sender_profile_link(
            msg.envelope.sender_id,
            sender_username=msg.envelope.sender_username,
        )
        sender_profile_text = self._build_sender_profile_text(
            sender_id=msg.envelope.sender_id,
            sender_username=msg.envelope.sender_username,
            sender_name=msg.envelope.sender_name,
        )
        publish_key = (msg.envelope.chat_id, msg.envelope.message_id)
        existing_publish = self._published_order_map.get(publish_key)
        status_label = "Yangilandi" if existing_publish else "Yangi"
        outbound = self.format_publish_message(
            raw_text=msg.envelope.raw_text,
            source_link=source_link,
            sender_profile_link=sender_profile_link,
            sender_profile_text=sender_profile_text,
            region_tag=decision.region_tag,
            status_label=status_label,
        )
        bot_entity_payload: tuple[str, list[dict[str, object]]] | None = None
        if (
            self.bot_publisher
            and (msg.envelope.sender_username or "").strip() == ""
            and (msg.envelope.sender_id or 0) > 0
        ):
            bot_entity_payload = self.format_publish_message_bot_entities(
                raw_text=msg.envelope.raw_text,
                source_link=source_link,
                region_tag=decision.region_tag,
                sender_id=msg.envelope.sender_id,
                sender_username=msg.envelope.sender_username,
                sender_name=msg.envelope.sender_name,
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
                    if bot_entity_payload and hasattr(self.bot_publisher, "edit_message_with_entities"):
                        entity_text, entity_list = bot_entity_payload
                        await self.bot_publisher.edit_message_with_entities(
                            chat_id=target_entity,
                            message_id=target_message_id,
                            text=entity_text,
                            entities=entity_list,
                        )
                    else:
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
                        parse_mode="html",
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

        target_entity = self.resolve_forward_target_for_chat(
            chat_id=msg.envelope.chat_id,
            chat_username=msg.envelope.chat_username,
            runtime_snapshot=runtime,
        )
        if target_entity is None:
            logger.info(
                "publish_skipped_no_route",
                extra={
                    "action": "publish_skip",
                    "chat_id": chat_id,
                    "message_id": msg.envelope.message_id,
                    "reason": "source_not_in_priority_routes",
                },
            )
            return
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
            if bot_entity_payload and hasattr(self.bot_publisher, "send_message_with_entities"):
                entity_text, entity_list = bot_entity_payload
                sent_message_id = await self.bot_publisher.send_message_with_entities(
                    chat_id=target_entity,
                    text=entity_text,
                    entities=entity_list,
                )
            else:
                sent_message_id = await self.bot_publisher.send_message(chat_id=target_entity, text=outbound)
        else:
            sent_message = await self.client.send_message(
                entity=target_entity,
                message=outbound,
                link_preview=False,
                parse_mode="html",
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
            updates = await self.client(functions.messages.ImportChatInviteRequest(invite_hash))
            joined_chat_id = self._extract_joined_chat_id(updates)
            if joined_chat_id is not None:
                self._remember_private_invite_source(invite_link, joined_chat_id)
                try:
                    await self.repository.upsert_private_invite_link(
                        invite_link,
                        source_chat_id=joined_chat_id,
                        note=None,
                        active=True,
                    )
                except Exception:
                    logger.exception(
                        "join_private_link_source_store_failed",
                        extra={"chat_id": joined_chat_id},
                    )
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

    def resolve_forward_target_for_chat(
        self,
        chat_id: int,
        chat_username: str | None,
        runtime_snapshot: object | None = None,
    ) -> str | int | None:
        runtime = runtime_snapshot if runtime_snapshot is not None else (
            self.runtime_config.snapshot() if self.runtime_config else None
        )
        default_target = runtime.forward_target if runtime else self.settings.forward_target
        second_target = (self.settings.forward_target_2 or "").strip()
        matches_priority_2 = self._matches_source_routes(
            self.settings.priority_group_links_2,
            chat_id=chat_id,
            chat_username=chat_username,
        )
        if matches_priority_2 and second_target:
            return self._resolve_forward_target(second_target)

        priority_only_enabled = bool(self.settings.forward_priority_only)
        has_priority_sources = bool(self.settings.priority_group_links or self.settings.priority_group_links_2)
        if priority_only_enabled and has_priority_sources:
            matches_priority_1 = self._matches_source_routes(
                self.settings.priority_group_links,
                chat_id=chat_id,
                chat_username=chat_username,
            )
            if matches_priority_1:
                return self._resolve_forward_target(default_target)
            if matches_priority_2:
                # If target2 is not configured, keep list2 on default target instead of dropping.
                return self._resolve_forward_target(default_target)
            return None
        return self._resolve_forward_target(default_target)

    def is_forward_destination_chat(
        self,
        chat_id: int,
        chat_username: str | None,
        runtime_snapshot: object | None = None,
    ) -> bool:
        runtime = runtime_snapshot if runtime_snapshot is not None else (
            self.runtime_config.snapshot() if self.runtime_config else None
        )
        default_target = runtime.forward_target if runtime else self.settings.forward_target
        if self._is_target_match(default_target, chat_id=chat_id, chat_username=chat_username):
            return True
        second_target = (self.settings.forward_target_2 or "").strip()
        if second_target and self._is_target_match(second_target, chat_id=chat_id, chat_username=chat_username):
            return True
        return False

    @staticmethod
    def _resolve_forward_target(target: str | int) -> str | int:
        if isinstance(target, int):
            return target
        value = str(target).strip()
        parsed_tme_ref = ActionExecutor._parse_tme_chat_ref(value)
        if parsed_tme_ref is not None:
            value = parsed_tme_ref
            if isinstance(value, int):
                return value
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        return value

    @staticmethod
    def _parse_tme_chat_ref(value: str) -> str | int | None:
        match = _TME_CHAT_REF_RE.fullmatch((value or "").strip())
        if not match:
            return None
        c_id = match.group("c_id")
        if c_id:
            return int(f"-100{c_id}")
        username = (match.group("username") or "").strip()
        if not username:
            return None
        lowered = username.lower()
        if lowered in {"joinchat"} or username.startswith("+"):
            return None
        return f"@{username}"

    def _is_source_route_match(
        self,
        source: str,
        chat_id: int,
        chat_username: str | None,
    ) -> bool:
        normalized_invite = self._normalize_private_invite_ref(source)
        if normalized_invite:
            mapped_chat_id = self._private_invite_route_map.get(normalized_invite)
            if mapped_chat_id is None:
                return False
            return chat_id == mapped_chat_id

        normalized_source = self._normalize_source_route_value(source)
        if normalized_source is None:
            return False
        if isinstance(normalized_source, int):
            return chat_id == normalized_source
        normalized_chat_username = (chat_username or "").strip().lstrip("@").lower()
        if not normalized_chat_username:
            return False
        return normalized_chat_username == normalized_source

    def _matches_source_routes(
        self,
        sources: tuple[str, ...],
        chat_id: int,
        chat_username: str | None,
    ) -> bool:
        if not sources:
            return False
        for source_ref in sources:
            if self._is_source_route_match(source_ref, chat_id=chat_id, chat_username=chat_username):
                return True
        return False

    @classmethod
    def _normalize_source_route_value(cls, value: str | int) -> str | int | None:
        if isinstance(value, int):
            return value
        raw = str(value).strip()
        if not raw:
            return None
        parsed_tme_ref = cls._parse_tme_chat_ref(raw)
        if parsed_tme_ref is not None:
            if isinstance(parsed_tme_ref, int):
                return parsed_tme_ref
            raw = parsed_tme_ref
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        normalized = raw.lstrip("@").strip().lower()
        return normalized or None

    @classmethod
    def _is_target_match(
        cls,
        target: str | int | None,
        chat_id: int,
        chat_username: str | None,
    ) -> bool:
        if target is None:
            return False
        resolved = cls._resolve_forward_target(target)
        if isinstance(resolved, int):
            return chat_id == resolved
        normalized_target = str(resolved).strip().lstrip("@").lower()
        if not normalized_target or normalized_target in {"me", "self"}:
            return False
        normalized_chat_username = (chat_username or "").strip().lstrip("@").lower()
        if not normalized_chat_username:
            return False
        return normalized_chat_username == normalized_target

    @staticmethod
    def _normalize_private_invite_ref(value: str | int) -> str | None:
        if isinstance(value, int):
            return None
        raw = str(value or "").strip()
        if not raw:
            return None
        match = _PRIVATE_INVITE_REF_RE.fullmatch(raw)
        if not match:
            return None
        invite_hash = (match.group("invite") or "").strip()
        if not invite_hash:
            return None
        return f"https://t.me/+{invite_hash}"

    def _remember_private_invite_source(self, invite_link: str, source_chat_id: int) -> None:
        normalized = self._normalize_private_invite_ref(invite_link)
        if normalized and source_chat_id:
            self._private_invite_route_map[normalized] = int(source_chat_id)

    async def refresh_private_invite_route_cache(self) -> int:
        rows = await self.repository.fetch_private_invite_rows(limit=5000)
        loaded = 0
        cache: dict[str, int] = {}
        for row in rows:
            if not row.active or row.source_chat_id is None:
                continue
            normalized = self._normalize_private_invite_ref(row.invite_link)
            if not normalized:
                continue
            cache[normalized] = int(row.source_chat_id)
            loaded += 1
        self._private_invite_route_map = cache
        if loaded:
            logger.info(
                "private_invite_route_cache_loaded",
                extra={"action": "route_cache_load", "count": loaded},
            )
        return loaded

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
    def _build_sender_profile_link(
        sender_id: int | None,
        sender_username: str | None = None,
    ) -> str:
        username = (sender_username or "").strip().lstrip("@")
        if username:
            return f"https://t.me/{username}"
        if not sender_id or sender_id <= 0:
            return ""
        return f"tg://user?id={sender_id}"

    @staticmethod
    def _build_sender_profile_text(
        sender_id: int | None,
        sender_username: str | None = None,
        sender_name: str | None = None,
    ) -> str:
        username = (sender_username or "").strip().lstrip("@")
        if username:
            return f"@{username}"
        display_name = " ".join((sender_name or "").split())
        if display_name:
            return display_name[:80]
        if sender_id and sender_id > 0:
            return "Profilga o'tish"
        return ""

    @staticmethod
    def format_publish_message(
        raw_text: str,
        source_link: str,
        region_tag: str | None,
        sender_profile_link: str = "",
        sender_profile_text: str = "",
        status_label: str = "Yangi",
    ) -> str:
        region = region_tag or "#Uzbekiston"
        body = (raw_text or "").strip() or "(matn topilmadi)"
        source_value = source_link or "private chat"

        # Limit based on visible/plain text length and keep headroom for HTML tags.
        tail_plain = f"\n\n{region}\n\nStatus: {status_label}\n\nManba: {source_value}"
        if sender_profile_link:
            tail_plain += "\n\nAloqa: profil"
        head_limit = max(120, 3900 - len(tail_plain) - 24)
        compact_body = (body[:head_limit] + "...") if len(body) > head_limit else body

        lines = [
            "<b>Taxi buyurtma:</b>",
            html.escape(compact_body, quote=False),
            html.escape(region, quote=False),
            f"<b>Status:</b> {html.escape(status_label, quote=False)}",
        ]
        if source_link:
            safe_source_href = html.escape(source_link, quote=True)
            safe_source_text = html.escape(source_link, quote=False)
            lines.append(f'Manba: <a href="{safe_source_href}">{safe_source_text}</a>')
        else:
            lines.append("Manba: private chat")
        if sender_profile_link:
            safe_sender_href = html.escape(sender_profile_link, quote=True)
            sender_label = sender_profile_text.strip() or "Profilga o'tish"
            safe_sender_text = html.escape(sender_label, quote=False)
            lines.append(f'Aloqa: <a href="{safe_sender_href}">{safe_sender_text}</a>')

        return "\n\n".join(lines)

    @staticmethod
    def format_publish_message_bot_entities(
        raw_text: str,
        source_link: str,
        region_tag: str | None,
        sender_id: int | None = None,
        sender_username: str | None = None,
        sender_name: str | None = None,
        status_label: str = "Yangi",
    ) -> tuple[str, list[dict[str, object]]]:
        region = region_tag or "#Uzbekiston"
        body = (raw_text or "").strip() or "(matn topilmadi)"
        source_value = source_link or "private chat"
        sender_label = ActionExecutor._build_sender_profile_text(
            sender_id=sender_id,
            sender_username=sender_username,
            sender_name=sender_name,
        )

        tail_plain = f"\n\n{region}\n\nStatus: {status_label}\n\nManba: {source_value}"
        if sender_label:
            tail_plain += f"\n\nAloqa: {sender_label}"
        head_limit = max(120, 3900 - len(tail_plain))
        compact_body = (body[:head_limit] + "...") if len(body) > head_limit else body

        text = ""
        entities: list[dict[str, object]] = []

        def _append(chunk: str) -> int:
            nonlocal text
            start = ActionExecutor._utf16_length(text)
            text += chunk
            return start

        def _append_line(chunk: str) -> int:
            if text:
                _append("\n\n")
            return _append(chunk)

        title_line = "Taxi buyurtma:"
        title_offset = _append_line(title_line)
        entities.append(
            {
                "type": "bold",
                "offset": title_offset,
                "length": ActionExecutor._utf16_length(title_line),
            }
        )

        _append_line(compact_body)
        _append_line(region)

        status_prefix = "Status: "
        status_line = f"{status_prefix}{status_label}"
        status_offset = _append_line(status_line)
        entities.append(
            {
                "type": "bold",
                "offset": status_offset,
                "length": ActionExecutor._utf16_length(status_prefix.rstrip()),
            }
        )

        source_prefix = "Manba: "
        source_line = f"{source_prefix}{source_value}"
        source_offset = _append_line(source_line)
        if source_link:
            entities.append(
                {
                    "type": "text_link",
                    "offset": source_offset + ActionExecutor._utf16_length(source_prefix),
                    "length": ActionExecutor._utf16_length(source_value),
                    "url": source_link,
                }
            )

        if sender_label:
            contact_prefix = "Aloqa: "
            contact_line = f"{contact_prefix}{sender_label}"
            contact_offset = _append_line(contact_line)
            username = (sender_username or "").strip().lstrip("@")
            mention_offset = contact_offset + ActionExecutor._utf16_length(contact_prefix)
            mention_length = ActionExecutor._utf16_length(sender_label)
            if username:
                entities.append(
                    {
                        "type": "text_link",
                        "offset": mention_offset,
                        "length": mention_length,
                        "url": f"https://t.me/{username}",
                    }
                )
            elif sender_id and sender_id > 0:
                entities.append(
                    {
                        "type": "text_mention",
                        "offset": mention_offset,
                        "length": mention_length,
                        "user": ActionExecutor._build_bot_api_text_mention_user(
                            sender_id=sender_id,
                            sender_name=sender_name,
                            fallback_label=sender_label,
                        ),
                    }
                )

        return text, entities

    @staticmethod
    def _build_bot_api_text_mention_user(
        sender_id: int,
        sender_name: str | None = None,
        fallback_label: str = "Foydalanuvchi",
    ) -> dict[str, object]:
        first_name = " ".join((sender_name or "").split()).strip()
        if not first_name:
            first_name = (fallback_label or "Foydalanuvchi").strip() or "Foydalanuvchi"
        return {
            "id": int(sender_id),
            "is_bot": False,
            "first_name": first_name[:64],
        }

    @staticmethod
    def _utf16_length(value: str) -> int:
        return len((value or "").encode("utf-16-le")) // 2

    @staticmethod
    def _extract_joined_chat_id(result: object) -> int | None:
        chats = getattr(result, "chats", None)
        if not chats:
            return None
        for chat in chats:
            try:
                peer_id = int(utils.get_peer_id(chat))
            except Exception:
                continue
            if peer_id != 0:
                return peer_id
        return None
