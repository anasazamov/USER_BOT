from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MessageEnvelope:
    chat_id: int
    message_id: int
    sender_id: int | None
    raw_text: str
    chat_username: str | None = None
    chat_title: str | None = None


@dataclass(slots=True)
class NormalizedMessage:
    envelope: MessageEnvelope
    normalized_text: str


@dataclass(slots=True)
class Decision:
    should_forward: bool
    should_reply: bool
    reply_text: str | None = None
    reason: str = ""
    region_tag: str | None = None
