# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Asynchronous Telethon-based **taxi-order userbot** for Uzbek-language Telegram groups. A single user-account session listens to many groups, classifies messages as taxi *orders* (keep) vs. *offers* (drop), and forwards matches to one or two target groups. A companion Bot-API bot publishes the forward and handles subscriber/admin commands. Primary language of data is Uzbek (Latin + Cyrillic, normalized to Latin).

## Common commands

PowerShell-first (this repo is developed on Windows):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main           # runs the userbot (Telethon login on first start)
pytest -q                    # full test suite
pytest tests/test_rules.py::test_some_case -q   # single test
```

Docker:

```bash
docker compose up --build -d      # full stack: userbot + postgres + redis
docker logs -f userbot            # tail JSON logs
```

There is **no linter, formatter, or type checker configured** — don't run `ruff`/`black`/`mypy` unless explicitly asked.

`.env` is loaded by `app/env.py` from the CWD upward; copy from `.env.example`. Only `TG_API_ID` and `TG_API_HASH` are strictly required to start, but a real run needs `DATABASE_URL`, `FORWARD_TARGET`, `OWNER_USER_ID`, optionally `TG_BOT_TOKEN`. See `README.md` for the full env catalog.

## Architecture (the big picture)

The entry point `app/main.py` wires everything in one async process and owns a **crash-restart loop at module bottom** controlled by `PROCESS_AUTO_RESTART` — exceptions out of `main()` cause the whole process to be re-`asyncio.run`'d after a backoff. Keep that in mind when adding shutdown logic; resources must close cleanly because they may be re-created.

Message pipeline (per incoming Telegram event):

```
Telethon NewMessage  →  TelegramUserbot._ingest_message
   →  text normalize (app/text.py: emoji strip, cyrillic→latin)
   →  FastFilter   (app/filtering.py: cheap keyword/regex pass)
   →  MessageQueue (app/message_queue.py: bounded asyncio.Queue)
   →  WorkerPool   (app/workers.py: N async workers)
   →  DecisionEngine (app/rules.py: order vs. offer classifier)
   →  CooldownManager (app/rate_limit.py: per-chat / global / join windows)
   →  ActionExecutor (app/actions.py: forward via Telethon or Bot API)
```

Things that don't fit the linear flow but are central:

- **`KeywordService` (app/keywords.py)** — DB-backed dynamic ruleset. `FastFilter` and `DecisionEngine` both call `_sync_dynamic_keywords()` lazily via a version counter so admin keyword edits propagate without restart. Kinds are fixed: `transport`, `request`, `offer`, `exclude`, `location`, `route` (see `KEYWORD_KINDS` in `app/storage/db.py`).
- **`RuntimeConfigService` (app/runtime_config.py)** — DB-persisted overlay on `Settings`. The list of overridable keys lives in `CONFIG_KEYS` and is mirrored by `RuntimeConfigSnapshot`. On startup, if `RUNTIME_CONFIG_SYNC_ENV_ON_STARTUP=true` (default), env values are written into the DB, overwriting prior runtime edits — adding a new tunable means: (1) field on `Settings.from_env`, (2) entry in `CONFIG_KEYS`, (3) field on `RuntimeConfigSnapshot`, (4) sync logic in `RuntimeConfigService`, (5) UI in `app/admin_web.py`.
- **Dual forward targets** — `FORWARD_TARGET` and `FORWARD_TARGET2` route messages from `PRIORITY_GROUP_LINKS` vs `PRIORITY_GROUP_LINKS_2` to different destinations. Routing decisions are made in `ActionExecutor.resolve_forward_target_for_chat`. When `FORWARD_PRIORITY_ONLY=true` (default), messages from non-priority groups are dropped.
- **Publish path is bimodal**: with `TG_BOT_TOKEN` set, `ActionExecutor.bot_publisher` is the `TelegramManagementBot` which talks to the Bot API via aiohttp; without it, the userbot account sends via Telethon. The `BotPublisher` Protocol in `app/actions.py` defines this contract.
- **Edit-aware deduplication** — `ActionExecutor._published_order_map` keys by `(source_chat_id, source_message_id)` so an edited source message updates the previously-published forward (`Status: Yangilandi`) rather than creating a duplicate.
- **Realtime vs history modes** — `REALTIME_ONLY=true` (default) means only live `NewMessage` events are processed. With it false and `HISTORY_SYNC_ENABLED=true`, `TelegramUserbot._history_sync_loop` periodically re-scans dialogs from a per-chat `chat_read_state` baseline; the baseline is captured on first sight so existing history is *not* backfilled.
- **Group discovery** (`app/group_discovery.py`) and **invite-link harvesting** (`app/invite_manager.py`) are background tasks owned by `main.py`. Both join groups subject to the `join_limit_day` cooldown; priority links from env are seeded in `priority_groups.py` and jump the queue.

## Storage

PostgreSQL via `asyncpg`. `app/storage/db.py` auto-creates the database if the user has `CREATEDB`, then applies `app/storage/schema.sql` on every startup (the schema is written idempotent with `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` — keep new migrations in this style). Tables: `action_log`, `private_invite_links`, `keyword_rules`, `runtime_config`, `message_audit`, `chat_read_state`, `discovered_groups`, `bot_subscribers`.

Redis (optional, via `REDIS_URL`) only backs `RedisWindowLimiter` for cross-process rate-limit windows. The default is `InMemoryWindowLimiter`; Redis failures fall back to memory automatically.

## Admin surfaces

- **Admin web UI**: aiohttp server in `app/admin_web.py`, port `ADMIN_WEB_PORT` (default 1311). Guarded by `ADMIN_WEB_TOKEN` query param when set. Endpoints under `/api/keywords`, `/api/groups`, `/api/config`.
- **Management bot**: `app/management_bot.py` long-polls Bot API. Implements `/start`/`/stop`/`/help` for subscribers, `/stats`/`/subscribers`/`/broadcast` for admins (allowlisted via `BOT_ADMIN_USER_IDS`), plus paid-subscription lifecycle and managed-private-group join-request approval.
- **Telethon commands**: `/kw list|reload|add|del <kind> <value>` from the owner's account (`OWNER_USER_ID`) in any chat the userbot sees.

## Logging

All logs are structured JSON to stdout (see `app/logging_setup.py`). Standard fields are `action`, `reason`, `chat_id`, `message_id`. Filter drops always include `chat_ref`, `chat_title`, `chat_username`, `raw_preview`, `normalized_preview` — when debugging "why didn't this message forward?", grep for `"action": "filter_drop"` or `"action": "decision_skip"` first.

## Testing notes

Tests live in `tests/` and run with plain `pytest`. They are unit-level — there is no integration harness against real Postgres/Telegram; the DB and Telethon client are stubbed/mocked per-test. New tests should follow that pattern rather than spinning up services.
