from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    api_id: int
    api_hash: str
    session_name: str = "taxi_userbot"

    database_url: str = "postgresql://postgres:postgres@localhost:5432/userbot"
    redis_url: Optional[str] = None

    log_level: str = "INFO"
    worker_count: int = 4
    queue_max_size: int = 2000
    owner_user_id: Optional[int] = None

    forward_target: str = "me"
    min_text_length: int = 18

    per_group_actions_hour: int = 15
    per_group_replies_10m: int = 3
    join_limit_day: int = 2
    global_actions_minute: int = 25

    min_human_delay_sec: float = 1.8
    max_human_delay_sec: float = 6.2
    worker_poll_timeout: float = 1.0
    invite_sync_interval_sec: int = 900
    discovery_enabled: bool = True
    discovery_interval_sec: int = 1800
    discovery_query_limit: int = 20
    discovery_join_batch: int = 4
    history_sync_enabled: bool = True
    history_sync_interval_sec: int = 300
    history_sync_batch_size: int = 120
    admin_web_enabled: bool = True
    admin_web_host: str = "0.0.0.0"
    admin_web_port: int = 1311
    admin_web_token: Optional[str] = None
    discovery_queries: tuple[str, ...] = (
        "taxi tashkent",
        "taksi toshkent",
        "taxi samarqand",
        "taxi andijon",
        "taxi namangan",
        "taxi fargona",
        "taxi buxoro",
        "taxi navoiy",
        "taxi qarshi",
        "taxi termiz",
        "taxi nukus",
        "taxi urganch",
        "yandex taxi uz",
    )

    @classmethod
    def from_env(cls) -> "Settings":
        api_id = int(os.environ["TG_API_ID"])
        api_hash = os.environ["TG_API_HASH"]
        return cls(
            api_id=api_id,
            api_hash=api_hash,
            session_name=os.environ.get("TG_SESSION_NAME", "taxi_userbot"),
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/userbot"
            ),
            redis_url=os.environ.get("REDIS_URL"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            worker_count=int(os.environ.get("WORKER_COUNT", "4")),
            queue_max_size=int(os.environ.get("QUEUE_MAX_SIZE", "2000")),
            owner_user_id=(
                int(os.environ["OWNER_USER_ID"]) if os.environ.get("OWNER_USER_ID") else None
            ),
            forward_target=os.environ.get("FORWARD_TARGET", "me"),
            min_text_length=int(os.environ.get("MIN_TEXT_LENGTH", "18")),
            per_group_actions_hour=int(os.environ.get("PER_GROUP_ACTIONS_HOUR", "15")),
            per_group_replies_10m=int(os.environ.get("PER_GROUP_REPLIES_10M", "3")),
            join_limit_day=int(os.environ.get("JOIN_LIMIT_DAY", "2")),
            global_actions_minute=int(os.environ.get("GLOBAL_ACTIONS_MINUTE", "25")),
            min_human_delay_sec=float(os.environ.get("MIN_HUMAN_DELAY_SEC", "1.8")),
            max_human_delay_sec=float(os.environ.get("MAX_HUMAN_DELAY_SEC", "6.2")),
            worker_poll_timeout=float(os.environ.get("WORKER_POLL_TIMEOUT", "1.0")),
            invite_sync_interval_sec=int(os.environ.get("INVITE_SYNC_INTERVAL_SEC", "900")),
            discovery_enabled=_parse_bool(os.environ.get("DISCOVERY_ENABLED", "true")),
            discovery_interval_sec=int(os.environ.get("DISCOVERY_INTERVAL_SEC", "1800")),
            discovery_query_limit=int(os.environ.get("DISCOVERY_QUERY_LIMIT", "20")),
            discovery_join_batch=int(os.environ.get("DISCOVERY_JOIN_BATCH", "4")),
            history_sync_enabled=_parse_bool(os.environ.get("HISTORY_SYNC_ENABLED", "true")),
            history_sync_interval_sec=int(os.environ.get("HISTORY_SYNC_INTERVAL_SEC", "300")),
            history_sync_batch_size=int(os.environ.get("HISTORY_SYNC_BATCH_SIZE", "120")),
            admin_web_enabled=_parse_bool(os.environ.get("ADMIN_WEB_ENABLED", "true")),
            admin_web_host=os.environ.get("ADMIN_WEB_HOST", "0.0.0.0"),
            admin_web_port=int(os.environ.get("ADMIN_WEB_PORT", "1311")),
            admin_web_token=os.environ.get("ADMIN_WEB_TOKEN"),
            discovery_queries=tuple(
                q.strip()
                for q in os.environ.get(
                    "DISCOVERY_QUERIES",
                    (
                        "taxi tashkent,taksi toshkent,taxi samarqand,taxi andijon,taxi namangan,"
                        "taxi fargona,taxi buxoro,taxi navoiy,taxi qarshi,taxi termiz,taxi nukus,"
                        "taxi urganch,yandex taxi uz"
                    ),
                ).split(",")
                if q.strip()
            ),
        )
