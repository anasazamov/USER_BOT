"""Read-only chat history exporter for classifier training.

Connects via a *clone* of the live Telethon session (see `clone_sqlite_session`)
so the production userbot is not disturbed. The script makes only `iter_messages`
calls — no edits, no joins, no read-acks — and disconnects within a few minutes
to keep the auth_key's foot-print on Telegram's side as small as possible.

Usage:
    python scripts/collect_training_data.py \\
        --chats "@Vodiy_Voha_taksi_xizmati,Test" \\
        --limit 1000 \\
        --output data/training_raw.jsonl

Safety:
- Use `--session-copy-from data/taxi_userbot` to clone the live session before
  connecting. The clone lives under `sessions/_clones/` and is disposable.
- Best practice: stop the production bot first (`docker compose stop userbot`),
  run this, then restart (`docker compose start userbot`). That eliminates the
  parallel-auth_key risk entirely.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Windows consoles default to cp1251 / cp866; chat titles often contain characters
# outside that range. Force UTF-8 for stdout/stderr so print() doesn't crash.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

from app.env import load_env_file
from app.text import normalize_text
from telethon import TelegramClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export message history from one or more chats to JSONL for classifier training.",
    )
    parser.add_argument(
        "--chats",
        required=True,
        help="Comma-separated chat refs: @username, t.me link, numeric id (-100...), or chat title.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Maximum messages per chat. Default: 1000",
    )
    parser.add_argument(
        "--output",
        default="data/training_raw.jsonl",
        help="Output JSONL path. Default: data/training_raw.jsonl",
    )
    parser.add_argument(
        "--session",
        default="data/taxi_userbot",
        help="Live Telethon session path (without .session suffix). Default: data/taxi_userbot",
    )
    parser.add_argument(
        "--clone-session",
        action="store_true",
        default=True,
        help="Clone the live session before connecting (RECOMMENDED — default on).",
    )
    parser.add_argument(
        "--no-clone-session",
        action="store_false",
        dest="clone_session",
        help="Use the live session file directly. Only safe if the live bot is stopped.",
    )
    parser.add_argument(
        "--min-text-length",
        type=int,
        default=5,
        help="Skip messages whose raw text is shorter than this. Default: 5",
    )
    return parser.parse_args()


def clone_sqlite_session(source_session: Path, clone_root: Path) -> Path:
    """Clone the SQLite session file so the live auth_key file isn't touched.

    The clone shares the same auth_key, so Telegram still sees it as the same
    session — but local file I/O does not race with the live bot's writes.
    """
    source_db = source_session if source_session.suffix == ".session" else source_session.with_suffix(".session")
    if not source_db.exists():
        raise SystemExit(f"Source session not found: {source_db}")

    clone_root.mkdir(parents=True, exist_ok=True)
    clone_dir = Path(tempfile.mkdtemp(prefix="train_clone_", dir=str(clone_root)))
    clone_db = clone_dir / source_db.name

    with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as src:
        with sqlite3.connect(clone_db) as dst:
            src.backup(dst)

    return clone_db.with_suffix("")


def chat_ref_candidates(chat_ref: str) -> list[str | int]:
    value = chat_ref.strip()
    candidates: list[str | int] = [value]
    if value.lstrip("-").isdigit():
        numeric = int(value)
        if numeric not in candidates:
            candidates.append(numeric)
        if numeric > 0:
            negative = int(f"-100{numeric}")
            if negative not in candidates:
                candidates.append(negative)
    return candidates


async def resolve_chat_entity(client: TelegramClient, chat_ref: str) -> object | None:
    for candidate in chat_ref_candidates(chat_ref):
        try:
            return await client.get_entity(candidate)
        except (TypeError, ValueError):
            continue
    # Fall back: search dialogs by title (case-insensitive substring).
    target = chat_ref.strip().lower()
    async for dialog in client.iter_dialogs():
        title = (getattr(dialog, "title", None) or "").lower()
        if target and target in title:
            return dialog.entity
    return None


async def export_chat(
    client: TelegramClient,
    entity: object,
    chat_ref: str,
    limit: int,
    min_text_length: int,
    out_fp,
) -> int:
    chat_id = int(getattr(entity, "id", 0))
    chat_title = getattr(entity, "title", "") or ""
    chat_username = getattr(entity, "username", None)

    written = 0
    async for message in client.iter_messages(entity, limit=limit):
        raw_text = (getattr(message, "raw_text", None) or getattr(message, "message", "") or "").strip()
        if len(raw_text) < min_text_length:
            continue

        sender = await message.get_sender() if message.sender_id else None
        sender_username = getattr(sender, "username", None) if sender else None
        sender_first_name = getattr(sender, "first_name", None) if sender else None

        record = {
            "chat_ref": chat_ref,
            "chat_id": chat_id,
            "chat_title": chat_title,
            "chat_username": chat_username,
            "message_id": int(message.id),
            "date": message.date.astimezone(timezone.utc).isoformat() if message.date else None,
            "sender_id": int(message.sender_id) if message.sender_id else None,
            "sender_username": sender_username,
            "sender_first_name": sender_first_name,
            "raw_text": raw_text,
            "normalized_text": normalize_text(raw_text),
        }
        out_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        written += 1
    return written


async def run() -> int:
    args = parse_args()

    load_env_file()
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("TG_API_ID/TG_API_HASH not found in env (.env or environment).")

    session_path = Path(args.session)
    if args.clone_session:
        clone_root = ROOT_DIR / "sessions" / "_clones"
        session_to_use = clone_sqlite_session(session_path, clone_root)
        print(f"[info] using session clone: {session_to_use}.session", flush=True)
    else:
        session_to_use = session_path
        print(f"[warn] using LIVE session: {session_to_use}.session — stop the bot first!", flush=True)

    chat_refs = [c.strip() for c in args.chats.split(",") if c.strip()]
    if not chat_refs:
        raise SystemExit("--chats is empty.")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    print(f"[info] starting collection at {started.isoformat()} — chats: {chat_refs}", flush=True)

    total = 0
    client = TelegramClient(str(session_to_use), int(api_id), api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Session is not authorized. Re-run the userbot interactively first.")

        with output_path.open("a", encoding="utf-8") as fp:
            for chat_ref in chat_refs:
                try:
                    entity = await resolve_chat_entity(client, chat_ref)
                    if entity is None:
                        print(f"[warn] could not resolve chat: {chat_ref!r} — skipping", flush=True)
                        continue
                    title = getattr(entity, "title", "") or ""
                    print(f"[info] reading {chat_ref!r} ({title!r}) — up to {args.limit} messages", flush=True)
                    written = await export_chat(
                        client, entity, chat_ref, args.limit, args.min_text_length, fp
                    )
                    total += written
                    print(f"[info]   -> {written} messages written", flush=True)
                except Exception as exc:
                    print(f"[error] failed for {chat_ref!r}: {exc!r} — continuing", flush=True)
                    continue
    finally:
        await client.disconnect()

    finished = datetime.now(timezone.utc)
    duration = (finished - started).total_seconds()
    print(
        f"[info] done — {total} messages -> {output_path} (elapsed {duration:.1f}s)",
        flush=True,
    )
    return 0


def main() -> None:
    try:
        exit_code = asyncio.run(run())
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
