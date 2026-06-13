from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
import time
from contextlib import suppress

from telethon import TelegramClient, errors, functions, types

from app.actions import ActionExecutor
from app.geo import GeoResolver
from app.runtime_config import RuntimeConfigService
from app.storage.db import ActionRepository, DiscoveredGroup

logger = logging.getLogger(__name__)
_QUERY_PRIORITY_TOKENS = (
    "samarqand",
    "samarkand",
    "toshkent",
    "tashkent",
    "fargona",
    "fergana",
    "andijon",
    "namangan",
    "vodiy",
)

# Direct taxi-domain keywords. Match anywhere inside title/username (lowercased, with
# underscores/dashes flattened to spaces). At least one match is required, OR the title
# must mention 2+ distinct UZ regions (route-pair convention like "urguttoshkint").
_TAXI_KEYWORDS: tuple[str, ...] = (
    "taxi", "taksi", "haydovchi", "shofer", "shafer",
    "transport", "ride", "yandex", "uber",
    "moshin", "mashin", "avto",
    "passajir", "yolovchi",
    "marshrut",
)

# Hard rejects — if any of these substrings appears in title/username, the group is
# never enqueued for join, regardless of taxi-keyword presence.
_TAXI_BLACKLIST_KEYWORDS: tuple[str, ...] = (
    "yangilik", "news",
    "kino", "film", "klip",
    "music", "muzika",
    "savdo", "sotuv", "sotiladi", "shop", "magazin", "market", "narx",
    "ovqat", "restoran", "cafe", "kafe", "pizza",
    "vakansiya", "ish topish",
    "kripto", "crypto", "bitcoin", "trading", "forex", "investitsiya",
    "qimor", "casino", "betting",
    "intim", "erotik", "18plus",
    "reklama kanal", "obuna ber",
)

_TITLE_SEPARATOR_RE = re.compile(r"[_\-./]+")

# Member-count window. Lower bound filters dead/test groups; upper bound filters mega
# dumps that are unlikely to be focused taxi channels. Zero/None means unknown — we
# stay lenient and accept (search results often omit participants_count).
_MIN_MEMBERS = 30
_MAX_MEMBERS = 200_000

# High-priority taxi corridors. Each iteration expands these into many query
# variants (`{a} {b} taksi`, `{a}dan {b}ga`, `{a} {b}`, both directions) so the
# crawler hits the same route from every angle Telegram's full-text search might
# match. Used to amplify the runtime-config `discovery_queries` list.
_PRIORITY_ROUTE_CORRIDORS: tuple[tuple[str, str], ...] = (
    ("samarqand", "toshkent"),
    ("samarqand", "vodiy"),
    ("samarqand", "fargona"),
    ("samarqand", "andijon"),
    ("samarqand", "namangan"),
    ("samarqand", "qoqon"),
    ("urgut", "toshkent"),
    ("urgut", "samarqand"),
    ("jartepa", "samarqand"),
)

_ROUTE_QUERY_TEMPLATES: tuple[str, ...] = (
    "{a} {b} taksi",
    "{a}dan {b}ga",
    "{a} {b}",
)

# Pacing between API calls so concurrent queries don't trip FloodWait. Telegram's
# contacts.search and messages.searchGlobal are loosely rate-limited; one second
# is the empirical sweet spot for sustained crawling without backoff.
_DISCOVERY_QUERY_SLEEP_SEC = 1.0


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
        active_hour_utc_start: int = 18,
        active_hour_utc_end: int = 2,
    ) -> None:
        self.client = client
        self.repository = repository
        self.executor = executor
        self.queries = queries
        self.interval_sec = interval_sec
        self.query_limit = query_limit
        self.join_batch = join_batch
        self.runtime_config = runtime_config
        self.active_hour_utc_start = active_hour_utc_start
        self.active_hour_utc_end = active_hour_utc_end
        self.geo = GeoResolver()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Per-RPC FloodWait deadlines. Telegram rate-limits SearchRequest and
        # SearchGlobalRequest independently; once a long wait (hours) is
        # returned, hammering the same endpoint just wastes outbound calls and
        # keeps the log noisy. Each deadline is reset when an attempt succeeds
        # past it, set when a FloodWaitError is raised.
        self._contacts_search_floodwait_until: float = 0.0
        self._messages_search_floodwait_until: float = 0.0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="group-discovery-manager")

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

    @staticmethod
    def _is_in_active_window(now_utc_hour: int, start: int, end: int) -> bool:
        """True if `now_utc_hour` is inside [start, end). The window may wrap
        midnight when end < start (e.g. 18-02 means 18:00-02:00 next day)."""
        if start == end:
            return True  # 24-hour window: always active
        if start < end:
            return start <= now_utc_hour < end
        return now_utc_hour >= start or now_utc_hour < end

    async def _run_iteration(self) -> bool:
        if not self.client.is_connected():
            return False
        if not await self._is_authorized():
            return False
        # Off-peak guard: discovery is expensive on Telegram's API budget and
        # competes with the realtime update stream. Skip it during daytime
        # taxi-traffic hours so the socket stays clear for receive+forward.
        now_utc_hour = _dt.datetime.now(_dt.timezone.utc).hour
        if not self._is_in_active_window(
            now_utc_hour, self.active_hour_utc_start, self.active_hour_utc_end
        ):
            logger.info(
                "group_discovery_off_hours",
                extra={
                    "action": "discovery",
                    "reason": (
                        f"now_utc_hour={now_utc_hour} "
                        f"active_window_utc={self.active_hour_utc_start}-{self.active_hour_utc_end}"
                    ),
                },
            )
            return True
        try:
            runtime = self.runtime_config.snapshot() if self.runtime_config else None
            if runtime and not runtime.discovery_enabled:
                logger.info(
                    "group_discovery_skipped",
                    extra={
                        "action": "discovery",
                        "reason": (
                            "disabled "
                            f"(runtime.discovery_enabled={runtime.discovery_enabled}, "
                            f"query_limit={runtime.discovery_query_limit}, "
                            f"join_batch={runtime.discovery_join_batch})"
                        ),
                    },
                )
                return True

            queries = runtime.discovery_queries if runtime else self.queries
            query_limit = runtime.discovery_query_limit if runtime else self.query_limit
            join_batch = runtime.discovery_join_batch if runtime else self.join_batch
            expanded_queries = self._expand_with_route_queries(queries)
            prioritized_queries = self._prioritize_queries(expanded_queries)
            logger.info(
                "group_discovery_iteration",
                extra={
                    "action": "discovery",
                    "count": len(prioritized_queries),
                    "reason": f"base={len(queries)} expanded={len(prioritized_queries)} limit={query_limit}",
                },
            )

            total_via_titles = 0
            total_via_messages = 0
            for query in prioritized_queries:
                total_via_titles += await self._discover_query(query, query_limit=query_limit)
                total_via_messages += await self._discover_query_via_messages(
                    query, query_limit=query_limit
                )
                await asyncio.sleep(_DISCOVERY_QUERY_SLEEP_SEC)
            logger.info(
                "group_discovery_iteration_done",
                extra={
                    "action": "discovery",
                    "reason": (
                        f"queries={len(prioritized_queries)} "
                        f"via_titles={total_via_titles} "
                        f"via_messages={total_via_messages}"
                    ),
                },
            )
            await self._join_pending(join_batch)
        except Exception:
            logger.exception("group_discovery_iteration_failed")
        return True

    async def _discover_query(self, query: str, query_limit: int | None = None) -> int:
        limit = query_limit if query_limit is not None else self.query_limit
        now = time.monotonic()
        if now < self._contacts_search_floodwait_until:
            return 0
        try:
            result = await self.client(functions.contacts.SearchRequest(q=query, limit=limit))
        except errors.FloodWaitError as exc:
            seconds = int(getattr(exc, "seconds", 60) or 60) + 5
            self._contacts_search_floodwait_until = time.monotonic() + seconds
            logger.warning(
                "discovery_query_floodwait",
                extra={
                    "action": "discovery_query",
                    "reason": f"contacts_search_floodwait={seconds}s",
                    "source_query": query,
                },
            )
            return 0
        except Exception as exc:
            logger.warning(
                "discovery_query_failed",
                extra={"action": "discovery_query", "reason": f"contacts_search:{exc!r}", "source_query": query},
            )
            return 0
        discovered = 0
        rejected_relevance = 0
        rejected_size = 0
        for chat in result.chats:
            if not isinstance(chat, types.Channel):
                continue
            if not (getattr(chat, "megagroup", False) or getattr(chat, "gigagroup", False)):
                continue

            relevant, relevance_reason = self._is_taxi_relevant(chat, self.geo)
            if not relevant:
                rejected_relevance += 1
                logger.info(
                    "discovery_chat_rejected",
                    extra={
                        "action": "discovery_reject",
                        "reason": relevance_reason,
                        "chat_id": int(chat.id),
                        "chat_title": chat.title or "",
                        "chat_username": chat.username or "",
                        "source_query": query,
                    },
                )
                continue

            size_ok, size_reason = self._is_size_appropriate(chat)
            if not size_ok:
                rejected_size += 1
                logger.info(
                    "discovery_chat_rejected",
                    extra={
                        "action": "discovery_reject",
                        "reason": size_reason,
                        "chat_id": int(chat.id),
                        "chat_title": chat.title or "",
                        "chat_username": chat.username or "",
                        "source_query": query,
                    },
                )
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
            extra={
                "action": "discovery_query",
                "reason": query,
                "count": discovered,
                "rejected_relevance": rejected_relevance,
                "rejected_size": rejected_size,
            },
        )
        return discovered

    async def _discover_query_via_messages(
        self, query: str, query_limit: int | None = None
    ) -> int:
        """Find groups by searching public *message content*, not titles.

        Telegram's contacts.search only matches title/bio/username — fast but narrow.
        messages.searchGlobal matches the body of recent public messages, so groups
        that don't have "taxi" in their name but are full of taxi orders still come
        back. We then run the same relevance filter on the source chat.
        """
        limit = max(min(query_limit or self.query_limit, 100), 1)
        now = time.monotonic()
        if now < self._messages_search_floodwait_until:
            return 0
        try:
            result = await self.client(
                functions.messages.SearchGlobalRequest(
                    q=query,
                    filter=types.InputMessagesFilterEmpty(),
                    min_date=0,
                    max_date=0,
                    offset_rate=0,
                    offset_peer=types.InputPeerEmpty(),
                    offset_id=0,
                    limit=limit,
                )
            )
        except errors.FloodWaitError as exc:
            seconds = int(getattr(exc, "seconds", 60) or 60) + 5
            self._messages_search_floodwait_until = time.monotonic() + seconds
            logger.warning(
                "discovery_query_floodwait",
                extra={
                    "action": "discovery_query_msg",
                    "reason": f"messages_search_floodwait={seconds}s",
                    "source_query": query,
                },
            )
            return 0
        except Exception as exc:
            logger.warning(
                "discovery_query_failed",
                extra={
                    "action": "discovery_query_msg",
                    "reason": f"messages_search:{exc!r}",
                    "source_query": query,
                },
            )
            return 0

        discovered = 0
        rejected_relevance = 0
        rejected_size = 0
        seen_peer_ids: set[int] = set()
        for chat in getattr(result, "chats", []) or []:
            if not isinstance(chat, types.Channel):
                continue
            if not (getattr(chat, "megagroup", False) or getattr(chat, "gigagroup", False)):
                continue
            if int(chat.id) in seen_peer_ids:
                continue
            seen_peer_ids.add(int(chat.id))

            relevant, relevance_reason = self._is_taxi_relevant(chat, self.geo)
            if not relevant:
                rejected_relevance += 1
                continue
            size_ok, _ = self._is_size_appropriate(chat)
            if not size_ok:
                rejected_size += 1
                continue

            username = chat.username if chat.username else None
            await self.repository.upsert_discovered_group(
                peer_id=int(chat.id),
                title=chat.title or "",
                username=username,
                source_query=f"msg:{query}",
                joined=not chat.left,
            )
            discovered += 1
        logger.info(
            "group_discovery_query_msg_done",
            extra={
                "action": "discovery_query_msg",
                "reason": query,
                "count": discovered,
                "rejected_relevance": rejected_relevance,
                "rejected_size": rejected_size,
            },
        )
        return discovered

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

    @staticmethod
    def _expand_with_route_queries(queries: tuple[str, ...]) -> tuple[str, ...]:
        """Combine user-provided queries with auto-generated route-pair variants.

        For each (a, b) in `_PRIORITY_ROUTE_CORRIDORS`, generate both directions
        and apply each query template, e.g. `samarqand toshkent`, `samarqand
        toshkent taksi`, `samarqanddan toshkentga`, `toshkent samarqand`, etc.
        Duplicates are removed; the user's explicit queries always lead.
        """
        seen: set[str] = set()
        expanded: list[str] = []
        for query in queries:
            normalized = query.strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                expanded.append(query)
        for a, b in _PRIORITY_ROUTE_CORRIDORS:
            for x, y in ((a, b), (b, a)):
                for template in _ROUTE_QUERY_TEMPLATES:
                    candidate = template.format(a=x, b=y).strip()
                    key = candidate.lower()
                    if key and key not in seen:
                        seen.add(key)
                        expanded.append(candidate)
        return tuple(expanded)

    @staticmethod
    def _prioritize_queries(queries: tuple[str, ...]) -> tuple[str, ...]:
        prioritized = sorted(
            queries,
            key=lambda query: (
                0
                if any(token in query.lower() for token in _QUERY_PRIORITY_TOKENS)
                else 1
            ),
        )
        return tuple(prioritized)

    @staticmethod
    def _normalize_haystack(chat: object) -> str:
        title = (getattr(chat, "title", None) or "").lower()
        username = (getattr(chat, "username", None) or "").lower()
        haystack = f" {title} {username} "
        return _TITLE_SEPARATOR_RE.sub(" ", haystack)

    @staticmethod
    def _count_distinct_regions(haystack: str, geo: GeoResolver) -> int:
        if not haystack:
            return 0
        found: set[str] = set()
        for phrase, region_name in geo._phrase_aliases:
            if phrase in haystack:
                found.add(region_name)
        for token in haystack.split():
            region_name = geo._single_alias_to_region.get(token)
            if region_name:
                found.add(region_name)
        return len(found)

    @classmethod
    def _is_taxi_relevant(cls, chat: object, geo: GeoResolver) -> tuple[bool, str]:
        haystack = cls._normalize_haystack(chat)
        for kw in _TAXI_BLACKLIST_KEYWORDS:
            if kw in haystack:
                return False, f"blacklist:{kw}"
        for kw in _TAXI_KEYWORDS:
            if kw in haystack:
                return True, f"taxi_keyword:{kw}"
        region_count = cls._count_distinct_regions(haystack, geo)
        if region_count >= 2:
            return True, f"route_pair:{region_count}_regions"
        return False, "no_taxi_signal"

    @staticmethod
    def _is_size_appropriate(chat: object) -> tuple[bool, str]:
        participants = getattr(chat, "participants_count", None) or 0
        if participants <= 0:
            return True, "size_unknown"
        if participants < _MIN_MEMBERS:
            return False, f"too_small:{participants}"
        if participants > _MAX_MEMBERS:
            return False, f"too_large:{participants}"
        return True, f"size_ok:{participants}"
