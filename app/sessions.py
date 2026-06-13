"""Helpers for creating secondary Telethon clients that share the main session.

The userbot runs on a single Telethon connection by default; under load, the
realtime update stream can fall behind because joins, discovery, and forwards
share the same MTProto socket. Splitting concerns across multiple clients —
each with its own connection but the same auth_key — keeps the update stream
unblocked while heavier RPCs are made elsewhere.

Each secondary client uses a *clone* of the main session's SQLite file. The
clone holds the same auth_key + DC info, so Telegram recognises it as the same
account; the parallel connection is treated as another device session on the
account. Only the main client subscribes to updates (`run_until_disconnected`);
secondaries only `connect()` and make API calls.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from telethon import TelegramClient

logger = logging.getLogger(__name__)


def _resolve_session_db_path(session_name: str) -> Path:
    base = Path(session_name)
    if base.suffix == ".session":
        return base
    return base.with_suffix(".session")


def _resolve_secondary_db_path(session_name: str, label: str) -> Path:
    base = _resolve_session_db_path(session_name)
    return base.with_name(f"{base.stem}_{label}.session")


def ensure_secondary_session(session_name: str, label: str) -> str:
    """Make sure a labelled secondary session SQLite file exists; return its
    bare path (without .session suffix) suitable for `TelegramClient(...)`.

    If the secondary file is missing, the main session is cloned into it via
    SQLite's backup API. If the main file is missing, we let Telethon raise
    on first use rather than masking the error here.
    """
    main_db = _resolve_session_db_path(session_name)
    secondary_db = _resolve_secondary_db_path(session_name, label)
    secondary_db.parent.mkdir(parents=True, exist_ok=True)

    if not secondary_db.exists():
        if not main_db.exists():
            logger.warning(
                "secondary_session_clone_skipped",
                extra={
                    "action": "session_clone",
                    "reason": f"main_missing:{main_db}",
                    "label": label,
                },
            )
            return str(secondary_db.with_suffix(""))
        with sqlite3.connect(f"file:{main_db}?mode=ro", uri=True) as src:
            with sqlite3.connect(secondary_db) as dst:
                src.backup(dst)
        logger.info(
            "secondary_session_cloned",
            extra={
                "action": "session_clone",
                "reason": f"from={main_db.name} to={secondary_db.name}",
                "label": label,
            },
        )
    return str(secondary_db.with_suffix(""))


async def create_secondary_client(
    session_name: str,
    api_id: int,
    api_hash: str,
    label: str,
) -> TelegramClient:
    """Build, connect, and authorize a secondary client. Returns the connected
    client. Caller is responsible for disconnecting on shutdown.

    The client does NOT subscribe to updates — it's intended for outbound API
    calls only. Update routing stays on the main client.
    """
    bare_path = ensure_secondary_session(session_name, label)
    client = TelegramClient(bare_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(f"secondary_client_not_authorized:{label}")
    logger.info(
        "secondary_client_connected",
        extra={"action": "session_connect", "label": label, "reason": bare_path},
    )
    return client
