from __future__ import annotations

import re
from dataclasses import dataclass

from app.storage.db import ActionRepository

_TME_LINK_RE = re.compile(r"^(?:https?://)?t\.me/(.+)$", flags=re.IGNORECASE)
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,32}$")
_INVITE_HASH_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


@dataclass(frozen=True, slots=True)
class ParsedPriorityGroupLink:
    invite_link: str | None = None
    username: str | None = None


def parse_priority_group_link(raw_link: str) -> ParsedPriorityGroupLink | None:
    link = (raw_link or "").strip()
    if not link:
        return None

    matched = _TME_LINK_RE.match(link)
    if not matched:
        return None

    path = matched.group(1).split("?", 1)[0].strip().rstrip("/")
    if not path:
        return None

    if path.startswith("+"):
        invite_hash = path[1:]
        if _is_valid_invite_hash(invite_hash):
            return ParsedPriorityGroupLink(invite_link=f"https://t.me/+{invite_hash}")
        return None

    if path.lower().startswith("joinchat/"):
        invite_hash = path.split("/", 1)[1].strip()
        if _is_valid_invite_hash(invite_hash):
            return ParsedPriorityGroupLink(invite_link=f"https://t.me/+{invite_hash}")
        return None

    segment = path.split("/", 1)[0].strip().lstrip("@")
    if _looks_like_invite_hash(segment):
        return ParsedPriorityGroupLink(invite_link=f"https://t.me/+{segment}")
    if _USERNAME_RE.fullmatch(segment):
        return ParsedPriorityGroupLink(username=segment.lower())
    return None


async def seed_priority_groups(repository: ActionRepository, links: tuple[str, ...]) -> tuple[int, int]:
    seeded_public = 0
    seeded_private = 0
    for link in links:
        parsed = parse_priority_group_link(link)
        if not parsed:
            continue
        if parsed.invite_link:
            await repository.upsert_private_invite_link(
                parsed.invite_link,
                note="priority_seed",
                active=True,
            )
            seeded_private += 1
            continue
        if parsed.username:
            await repository.upsert_public_group_username(
                parsed.username,
                title="priority_seed",
                source_query="priority_seed",
            )
            seeded_public += 1
    return seeded_public, seeded_private


def _is_valid_invite_hash(value: str) -> bool:
    return bool(_INVITE_HASH_RE.fullmatch(value))


def _looks_like_invite_hash(value: str) -> bool:
    if "_" in value:
        return False
    if not _is_valid_invite_hash(value):
        return False
    has_lower = any(char.islower() for char in value)
    has_upper = any(char.isupper() for char in value)
    has_digit = any(char.isdigit() for char in value)
    return has_lower and has_upper and has_digit
