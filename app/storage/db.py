from __future__ import annotations

import logging
import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg

KEYWORD_KINDS: tuple[str, ...] = ("transport", "request", "offer", "exclude", "location", "route")
_SAFE_DB_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
logger = logging.getLogger(__name__)


def _extract_database_name_from_dsn(dsn: str) -> str | None:
    parsed = urlsplit(dsn)
    if not parsed.scheme or not parsed.netloc:
        return None
    path = (parsed.path or "").lstrip("/")
    if not path:
        return None
    # Postgres URL paths are usually "/<database>".
    return path.split("/", 1)[0] or None


def _replace_database_name_in_dsn(dsn: str, db_name: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit((parsed.scheme, parsed.netloc, f"/{db_name}", parsed.query, parsed.fragment))


def _is_safe_database_name(db_name: str) -> bool:
    return bool(_SAFE_DB_NAME_RE.fullmatch(db_name))


class Postgres:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        try:
            self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=4)
        except asyncpg.InvalidCatalogNameError:
            created = await self._create_database_if_missing()
            if not created:
                raise
            self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=4)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def apply_schema(self) -> None:
        if not self.pool:
            raise RuntimeError("db_not_connected")
        sql = Path("app/storage/schema.sql").read_text(encoding="utf-8")
        async with self.pool.acquire() as conn:
            await conn.execute(sql)

    async def _create_database_if_missing(self) -> bool:
        db_name = _extract_database_name_from_dsn(self.dsn)
        if not db_name:
            return False
        if db_name.lower() in {"postgres", "template0", "template1"}:
            return False
        if not _is_safe_database_name(db_name):
            logger.warning("database_name_unsafe_skip_autocreate", extra={"database": db_name})
            return False

        admin_dsn = _replace_database_name_in_dsn(self.dsn, "postgres")
        conn = await asyncpg.connect(dsn=admin_dsn)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
            if exists:
                return True
            quoted = db_name.replace('"', '""')
            await conn.execute(f'CREATE DATABASE "{quoted}"')
            logger.info("database_auto_created", extra={"database": db_name})
            return True
        finally:
            await conn.close()


@dataclass(slots=True)
class DiscoveredGroup:
    peer_id: int
    username: str
    title: str
    active: bool
    joined: bool
    source_query: str
    last_error: str | None


@dataclass(slots=True)
class PrivateInviteLink:
    invite_link: str
    active: bool
    source_chat_id: int | None
    last_seen_at: str


@dataclass(slots=True)
class BotSubscriber:
    user_id: int
    chat_id: int
    username: str | None
    first_name: str | None
    active: bool
    subscribed_at: str
    updated_at: str


@dataclass(slots=True)
class ActionStats:
    published_1h: int
    published_24h: int
    edited_24h: int
    joins_24h: int
    errors_24h: int
    total_actions_24h: int


def _manual_peer_id(username: str) -> int:
    key = username.strip().lower().lstrip("@")
    crc = zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF
    return -(9_000_000_000 + crc)


class ActionRepository:
    def __init__(self, db: Postgres) -> None:
        self.db = db

    async def insert_action(self, chat_id: int, message_id: int, action: str, status: str) -> None:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        INSERT INTO action_log (chat_id, message_id, action_type, status)
        VALUES ($1, $2, $3, $4)
        """
        async with self.db.pool.acquire() as conn:
            await conn.execute(query, chat_id, message_id, action, status)

    async def fetch_active_invite_links(self) -> list[str]:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        SELECT invite_link
        FROM private_invite_links
        WHERE active = TRUE
        ORDER BY
          CASE WHEN note = 'priority_seed' THEN 0 ELSE 1 END,
          last_seen_at DESC
        """
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(query)
        return [row["invite_link"] for row in rows]

    async def fetch_private_invite_rows(self, limit: int = 300) -> list[PrivateInviteLink]:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        SELECT invite_link, active, source_chat_id, last_seen_at
        FROM private_invite_links
        ORDER BY last_seen_at DESC
        LIMIT $1
        """
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(query, limit)
        return [
            PrivateInviteLink(
                invite_link=row["invite_link"],
                active=bool(row["active"]),
                source_chat_id=row["source_chat_id"],
                last_seen_at=str(row["last_seen_at"]),
            )
            for row in rows
        ]

    async def upsert_private_invite_link(
        self,
        invite_link: str,
        source_chat_id: int | None = None,
        note: str = "auto_discovered",
        active: bool = True,
    ) -> None:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        INSERT INTO private_invite_links (invite_link, active, note, source_chat_id, last_seen_at)
        VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (invite_link) DO UPDATE SET
            active = EXCLUDED.active,
            note = COALESCE(EXCLUDED.note, private_invite_links.note),
            source_chat_id = COALESCE(EXCLUDED.source_chat_id, private_invite_links.source_chat_id),
            last_seen_at = NOW()
        """
        async with self.db.pool.acquire() as conn:
            await conn.execute(query, invite_link, active, note, source_chat_id)

    async def set_private_invite_active(self, invite_link: str, active: bool) -> bool:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = "UPDATE private_invite_links SET active = $2, last_seen_at = NOW() WHERE invite_link = $1"
        async with self.db.pool.acquire() as conn:
            status = await conn.execute(query, invite_link, active)
        return status.endswith("1")

    async def delete_private_invite(self, invite_link: str) -> bool:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = "DELETE FROM private_invite_links WHERE invite_link = $1"
        async with self.db.pool.acquire() as conn:
            status = await conn.execute(query, invite_link)
        return status.endswith("1")

    async def ensure_default_keyword_rules(self, defaults: dict[str, set[str]]) -> None:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        insert_query = """
        INSERT INTO keyword_rules (kind, value)
        VALUES ($1, $2)
        ON CONFLICT (kind, value) DO NOTHING
        """
        async with self.db.pool.acquire() as conn:
            for kind, values in defaults.items():
                if kind not in KEYWORD_KINDS:
                    continue
                for value in values:
                    await conn.execute(insert_query, kind, value)

    async def fetch_keyword_rules(self) -> dict[str, set[str]]:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = "SELECT kind, value FROM keyword_rules"
        grouped: dict[str, set[str]] = {kind: set() for kind in KEYWORD_KINDS}
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(query)
        for row in rows:
            kind = row["kind"]
            value = row["value"]
            if kind in grouped:
                grouped[kind].add(value)
        return grouped

    async def upsert_keyword_rule(self, kind: str, value: str) -> None:
        if kind not in KEYWORD_KINDS:
            raise ValueError("invalid_keyword_kind")
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        INSERT INTO keyword_rules (kind, value)
        VALUES ($1, $2)
        ON CONFLICT (kind, value) DO NOTHING
        """
        async with self.db.pool.acquire() as conn:
            await conn.execute(query, kind, value)

    async def delete_keyword_rule(self, kind: str, value: str) -> bool:
        if kind not in KEYWORD_KINDS:
            raise ValueError("invalid_keyword_kind")
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = "DELETE FROM keyword_rules WHERE kind = $1 AND value = $2"
        async with self.db.pool.acquire() as conn:
            status = await conn.execute(query, kind, value)
        return status.endswith("1")

    async def fetch_runtime_config(self) -> dict[str, str]:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = "SELECT key, value FROM runtime_config"
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(query)
        return {str(row["key"]): str(row["value"]) for row in rows}

    async def upsert_runtime_config(self, key: str, value: str) -> None:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        INSERT INTO runtime_config (key, value, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (key) DO UPDATE SET
            value = EXCLUDED.value,
            updated_at = NOW()
        """
        async with self.db.pool.acquire() as conn:
            await conn.execute(query, key, value)

    async def delete_runtime_config(self, key: str) -> bool:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = "DELETE FROM runtime_config WHERE key = $1"
        async with self.db.pool.acquire() as conn:
            status = await conn.execute(query, key)
        return status.endswith("1")

    async def fetch_chat_read_states(self) -> dict[int, int]:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = "SELECT chat_id, last_seen_message_id FROM chat_read_state"
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(query)
        return {int(row["chat_id"]): int(row["last_seen_message_id"]) for row in rows}

    async def upsert_chat_read_state(self, chat_id: int, last_seen_message_id: int) -> None:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        INSERT INTO chat_read_state (chat_id, last_seen_message_id, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (chat_id) DO UPDATE SET
            last_seen_message_id = GREATEST(chat_read_state.last_seen_message_id, EXCLUDED.last_seen_message_id),
            updated_at = NOW()
        """
        async with self.db.pool.acquire() as conn:
            await conn.execute(query, chat_id, last_seen_message_id)

    async def upsert_discovered_group(
        self,
        peer_id: int,
        title: str,
        username: str | None,
        source_query: str,
        joined: bool,
        active: bool = True,
    ) -> None:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        clean_username = username.strip().lstrip("@").lower() if username else None
        query = """
        INSERT INTO discovered_groups (peer_id, title, username, source_query, joined, active)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (peer_id) DO UPDATE SET
            title = EXCLUDED.title,
            username = COALESCE(EXCLUDED.username, discovered_groups.username),
            source_query = EXCLUDED.source_query,
            joined = discovered_groups.joined OR EXCLUDED.joined,
            active = discovered_groups.active OR EXCLUDED.active,
            updated_at = NOW(),
            last_error = NULL
        """
        async with self.db.pool.acquire() as conn:
            await conn.execute(query, peer_id, title, clean_username, source_query, joined, active)

    async def upsert_public_group_username(
        self,
        username: str,
        title: str = "admin_manual",
        source_query: str = "admin_manual",
    ) -> int:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        clean_username = username.strip().lstrip("@").lower()
        if not clean_username:
            raise ValueError("empty_username")

        # If group already exists by username, reactivate it.
        select_query = "SELECT peer_id FROM discovered_groups WHERE username = $1 LIMIT 1"
        update_query = """
        UPDATE discovered_groups
        SET active = TRUE, updated_at = NOW(), last_error = NULL, source_query = $2
        WHERE peer_id = $1
        """
        insert_query = """
        INSERT INTO discovered_groups (peer_id, title, username, source_query, joined, active)
        VALUES ($1, $2, $3, $4, FALSE, TRUE)
        ON CONFLICT (peer_id) DO UPDATE SET
            title = EXCLUDED.title,
            username = EXCLUDED.username,
            source_query = EXCLUDED.source_query,
            active = TRUE,
            updated_at = NOW(),
            last_error = NULL
        """
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(select_query, clean_username)
            if row:
                peer_id = int(row["peer_id"])
                await conn.execute(update_query, peer_id, source_query)
                return peer_id

            peer_id = _manual_peer_id(clean_username)
            await conn.execute(insert_query, peer_id, title, clean_username, source_query)
            return peer_id

    async def fetch_public_groups(self, limit: int = 300) -> list[DiscoveredGroup]:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        SELECT peer_id, username, title, active, joined, source_query, last_error
        FROM discovered_groups
        ORDER BY updated_at DESC
        LIMIT $1
        """
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(query, limit)
        return [
            DiscoveredGroup(
                peer_id=int(row["peer_id"]),
                username=row["username"] or "",
                title=row["title"] or "",
                active=bool(row["active"]),
                joined=bool(row["joined"]),
                source_query=row["source_query"] or "",
                last_error=row["last_error"],
            )
            for row in rows
        ]

    async def fetch_unjoined_public_groups(self, limit: int) -> list[DiscoveredGroup]:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        SELECT peer_id, username, title, active, joined, source_query, last_error
        FROM discovered_groups
        WHERE joined = FALSE
          AND active = TRUE
          AND username IS NOT NULL
          AND username <> ''
        ORDER BY
          CASE WHEN source_query = 'priority_seed' THEN 0 ELSE 1 END,
          updated_at DESC
        LIMIT $1
        """
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(query, limit)
        return [
            DiscoveredGroup(
                peer_id=int(row["peer_id"]),
                username=row["username"] or "",
                title=row["title"] or "",
                active=bool(row["active"]),
                joined=bool(row["joined"]),
                source_query=row["source_query"] or "",
                last_error=row["last_error"],
            )
            for row in rows
        ]

    async def set_public_group_active(self, username: str, active: bool) -> bool:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        clean_username = username.strip().lstrip("@").lower()
        query = """
        UPDATE discovered_groups
        SET active = $2, updated_at = NOW()
        WHERE username = $1
        """
        async with self.db.pool.acquire() as conn:
            status = await conn.execute(query, clean_username, active)
        return status.endswith("1")

    async def delete_public_group(self, username: str) -> bool:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        clean_username = username.strip().lstrip("@").lower()
        query = "DELETE FROM discovered_groups WHERE username = $1"
        async with self.db.pool.acquire() as conn:
            status = await conn.execute(query, clean_username)
        return status.endswith("1")

    async def upsert_bot_subscriber(
        self,
        user_id: int,
        chat_id: int,
        username: str | None,
        first_name: str | None,
        active: bool = True,
    ) -> None:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        clean_username = username.strip().lstrip("@").lower() if username else None
        query = """
        INSERT INTO bot_subscribers (user_id, chat_id, username, first_name, active, subscribed_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
        ON CONFLICT (user_id) DO UPDATE SET
            chat_id = EXCLUDED.chat_id,
            username = COALESCE(EXCLUDED.username, bot_subscribers.username),
            first_name = COALESCE(EXCLUDED.first_name, bot_subscribers.first_name),
            active = EXCLUDED.active,
            updated_at = NOW()
        """
        async with self.db.pool.acquire() as conn:
            await conn.execute(query, user_id, chat_id, clean_username, first_name, active)

    async def set_bot_subscriber_active(self, user_id: int, active: bool) -> bool:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        UPDATE bot_subscribers
        SET active = $2, updated_at = NOW()
        WHERE user_id = $1
        """
        async with self.db.pool.acquire() as conn:
            status = await conn.execute(query, user_id, active)
        return status.endswith("1")

    async def count_bot_subscribers(self, active_only: bool = True) -> int:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = (
            "SELECT COUNT(*) FROM bot_subscribers WHERE active = TRUE"
            if active_only
            else "SELECT COUNT(*) FROM bot_subscribers"
        )
        async with self.db.pool.acquire() as conn:
            value = await conn.fetchval(query)
        return int(value or 0)

    async def fetch_active_subscriber_chat_ids(self, limit: int = 5000) -> list[int]:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        SELECT chat_id
        FROM bot_subscribers
        WHERE active = TRUE
        ORDER BY updated_at DESC
        LIMIT $1
        """
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(query, limit)
        return [int(row["chat_id"]) for row in rows]

    async def fetch_bot_subscribers(self, limit: int = 50, active_only: bool = True) -> list[BotSubscriber]:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        where_clause = "WHERE active = TRUE" if active_only else ""
        query = f"""
        SELECT user_id, chat_id, username, first_name, active, subscribed_at, updated_at
        FROM bot_subscribers
        {where_clause}
        ORDER BY updated_at DESC
        LIMIT $1
        """
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(query, limit)
        return [
            BotSubscriber(
                user_id=int(row["user_id"]),
                chat_id=int(row["chat_id"]),
                username=row["username"],
                first_name=row["first_name"],
                active=bool(row["active"]),
                subscribed_at=str(row["subscribed_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    async def fetch_action_stats(self) -> ActionStats:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        SELECT
            COUNT(*) FILTER (
                WHERE action_type = 'publish'
                  AND created_at >= NOW() - INTERVAL '1 hour'
            ) AS published_1h,
            COUNT(*) FILTER (
                WHERE action_type = 'publish'
                  AND created_at >= NOW() - INTERVAL '24 hour'
            ) AS published_24h,
            COUNT(*) FILTER (
                WHERE action_type = 'publish_edit'
                  AND created_at >= NOW() - INTERVAL '24 hour'
            ) AS edited_24h,
            COUNT(*) FILTER (
                WHERE action_type IN ('join', 'join_public')
                  AND created_at >= NOW() - INTERVAL '24 hour'
            ) AS joins_24h,
            COUNT(*) FILTER (
                WHERE status = 'error'
                  AND created_at >= NOW() - INTERVAL '24 hour'
            ) AS errors_24h,
            COUNT(*) FILTER (
                WHERE created_at >= NOW() - INTERVAL '24 hour'
            ) AS total_actions_24h
        FROM action_log
        """
        async with self.db.pool.acquire() as conn:
            row = await conn.fetchrow(query)
        if row is None:
            return ActionStats(0, 0, 0, 0, 0, 0)
        return ActionStats(
            published_1h=int(row["published_1h"] or 0),
            published_24h=int(row["published_24h"] or 0),
            edited_24h=int(row["edited_24h"] or 0),
            joins_24h=int(row["joins_24h"] or 0),
            errors_24h=int(row["errors_24h"] or 0),
            total_actions_24h=int(row["total_actions_24h"] or 0),
        )

    async def mark_group_joined(self, peer_id: int) -> None:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        UPDATE discovered_groups
        SET joined = TRUE, updated_at = NOW(), last_error = NULL
        WHERE peer_id = $1
        """
        async with self.db.pool.acquire() as conn:
            await conn.execute(query, peer_id)

    async def mark_group_error(self, peer_id: int, error: str) -> None:
        if not self.db.pool:
            raise RuntimeError("db_not_connected")
        query = """
        UPDATE discovered_groups
        SET updated_at = NOW(), last_error = $2
        WHERE peer_id = $1
        """
        async with self.db.pool.acquire() as conn:
            await conn.execute(query, peer_id, error[:500])
