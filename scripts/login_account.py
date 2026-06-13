"""Interactive Telethon login for a *separate* Telegram account.

Use this to authorize a secondary account whose session file the userbot will
load via TG_DISCOVERY_SESSION_NAME or TG_JOIN_SESSION_NAME. Each secondary
account has its own auth_key, so Telegram treats it as an independent device:
updates go to it independently, rate-limits are counted independently.

Usage:
    # Inside the container or wherever Python can reach Telegram:
    python scripts/login_account.py \\
        --session data/discovery_userbot \\
        --phone +99890XXXXXXX

You'll be prompted for the SMS code Telegram sends to that phone, plus the
2FA password if the account has one set. On success, the .session file lands
at the path you passed; then set the matching env var in .env and restart.

The script uses TG_API_ID / TG_API_HASH from env (or .env). It is intentionally
interactive — Telegram requires a real human to enter the SMS code.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.env import load_env_file
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authorize a separate Telegram account into a Telethon session file.",
    )
    parser.add_argument(
        "--session",
        required=True,
        help="Session path WITHOUT the .session suffix (e.g. data/discovery_userbot).",
    )
    parser.add_argument(
        "--phone",
        help="Phone number for this account (e.g. +99890XXXXXXX). If omitted, you'll be prompted.",
    )
    parser.add_argument(
        "--password",
        help="2FA password if the account has one. If omitted, you'll be prompted only when needed.",
    )
    return parser.parse_args()


async def run() -> int:
    args = parse_args()
    load_env_file()

    api_id_raw = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id_raw or not api_hash:
        print("[error] TG_API_ID / TG_API_HASH not set (check .env)", file=sys.stderr)
        return 2
    api_id = int(api_id_raw)

    session_path = args.session
    if session_path.endswith(".session"):
        session_path = session_path[: -len(".session")]
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)

    print(f"[info] session path: {session_path}.session")
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"[ok] already authorized as @{me.username} (id={me.id})")
            return 0

        phone = args.phone or input("Phone number (e.g. +998901234567): ").strip()
        sent = await client.send_code_request(phone)
        # Try the code a few times in case of typos.
        for attempt in range(3):
            code = input(f"SMS code for {phone}: ").strip()
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                break
            except SessionPasswordNeededError:
                pwd = args.password or input("2FA password: ").strip()
                await client.sign_in(password=pwd)
                break
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
                print(f"[warn] {exc!r}; try again", file=sys.stderr)
                if attempt == 2:
                    print("[error] giving up after 3 tries", file=sys.stderr)
                    return 3

        me = await client.get_me()
        print(f"[ok] logged in as @{me.username} (id={me.id})")
        print(f"[next] set TG_DISCOVERY_SESSION_NAME or TG_JOIN_SESSION_NAME to '{session_path}' in .env, then restart the bot.")
        return 0
    finally:
        await client.disconnect()


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
