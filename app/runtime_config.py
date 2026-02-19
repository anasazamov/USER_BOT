from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.storage.db import ActionRepository

logger = logging.getLogger(__name__)

CONFIG_KEYS: tuple[str, ...] = (
    "forward_target",
    "min_text_length",
    "per_group_actions_hour",
    "per_group_replies_10m",
    "join_limit_day",
    "global_actions_minute",
    "min_human_delay_sec",
    "max_human_delay_sec",
    "discovery_enabled",
    "discovery_query_limit",
    "discovery_join_batch",
    "discovery_queries",
)


@dataclass(frozen=True, slots=True)
class RuntimeConfigSnapshot:
    forward_target: str
    min_text_length: int
    per_group_actions_hour: int
    per_group_replies_10m: int
    join_limit_day: int
    global_actions_minute: int
    min_human_delay_sec: float
    max_human_delay_sec: float
    discovery_enabled: bool
    discovery_query_limit: int
    discovery_join_batch: int
    discovery_queries: tuple[str, ...]
    version: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "forward_target": self.forward_target,
            "min_text_length": self.min_text_length,
            "per_group_actions_hour": self.per_group_actions_hour,
            "per_group_replies_10m": self.per_group_replies_10m,
            "join_limit_day": self.join_limit_day,
            "global_actions_minute": self.global_actions_minute,
            "min_human_delay_sec": self.min_human_delay_sec,
            "max_human_delay_sec": self.max_human_delay_sec,
            "discovery_enabled": self.discovery_enabled,
            "discovery_query_limit": self.discovery_query_limit,
            "discovery_join_batch": self.discovery_join_batch,
            "discovery_queries": list(self.discovery_queries),
            "version": self.version,
        }


class RuntimeConfigService:
    def __init__(self, settings: Settings, repository: ActionRepository) -> None:
        self.settings = settings
        self.repository = repository
        self._lock = asyncio.Lock()
        self._version = 0
        self._snapshot = RuntimeConfigSnapshot(
            forward_target=settings.forward_target,
            min_text_length=settings.min_text_length,
            per_group_actions_hour=settings.per_group_actions_hour,
            per_group_replies_10m=settings.per_group_replies_10m,
            join_limit_day=settings.join_limit_day,
            global_actions_minute=settings.global_actions_minute,
            min_human_delay_sec=settings.min_human_delay_sec,
            max_human_delay_sec=settings.max_human_delay_sec,
            discovery_enabled=settings.discovery_enabled,
            discovery_query_limit=settings.discovery_query_limit,
            discovery_join_batch=settings.discovery_join_batch,
            discovery_queries=settings.discovery_queries,
            version=0,
        )

    async def initialize(self) -> None:
        async with self._lock:
            stored = await self.repository.fetch_runtime_config()
            current = self._snapshot.as_json()
            for key, value in stored.items():
                if key not in CONFIG_KEYS:
                    continue
                try:
                    current[key] = self._parse_value(key, value)
                except ValueError:
                    logger.warning("runtime_config_invalid_value", extra={"config_key": key})

            snapshot = self._build_snapshot(current)
            self._version += 1
            self._snapshot = RuntimeConfigSnapshot(**snapshot, version=self._version)

    def snapshot(self) -> RuntimeConfigSnapshot:
        return self._snapshot

    async def list_config(self) -> dict[str, Any]:
        return self.snapshot().as_json()

    async def set_value(self, key: str, value: Any) -> RuntimeConfigSnapshot:
        if key not in CONFIG_KEYS:
            raise ValueError("invalid_config_key")

        async with self._lock:
            current = self._snapshot.as_json()
            parsed = self._parse_value(key, value)
            current[key] = parsed
            snapshot = self._build_snapshot(current)
            self._version += 1
            self._snapshot = RuntimeConfigSnapshot(**snapshot, version=self._version)
            await self.repository.upsert_runtime_config(key, self._serialize_value(key, parsed))
            return self._snapshot

    async def set_many(self, payload: dict[str, Any]) -> RuntimeConfigSnapshot:
        unknown = [key for key in payload if key not in CONFIG_KEYS]
        if unknown:
            raise ValueError(f"invalid_config_key:{unknown[0]}")

        async with self._lock:
            current = self._snapshot.as_json()
            parsed_values: dict[str, Any] = {}
            for key, value in payload.items():
                parsed_values[key] = self._parse_value(key, value)
                current[key] = parsed_values[key]

            snapshot = self._build_snapshot(current)
            self._version += 1
            self._snapshot = RuntimeConfigSnapshot(**snapshot, version=self._version)

            for key, parsed in parsed_values.items():
                await self.repository.upsert_runtime_config(key, self._serialize_value(key, parsed))
            return self._snapshot

    @staticmethod
    def _parse_bool(raw: Any) -> bool:
        if isinstance(raw, bool):
            return raw
        val = str(raw).strip().lower()
        if val in {"1", "true", "yes", "on"}:
            return True
        if val in {"0", "false", "no", "off"}:
            return False
        raise ValueError("invalid_bool")

    @staticmethod
    def _parse_int(raw: Any, min_value: int, max_value: int) -> int:
        value = int(str(raw).strip())
        if value < min_value or value > max_value:
            raise ValueError("out_of_range")
        return value

    @staticmethod
    def _parse_float(raw: Any, min_value: float, max_value: float) -> float:
        value = float(str(raw).strip())
        if value < min_value or value > max_value:
            raise ValueError("out_of_range")
        return value

    @staticmethod
    def _parse_queries(raw: Any) -> tuple[str, ...]:
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw]
        else:
            text = str(raw or "")
            values = []
            for chunk in text.replace("\r", "").split("\n"):
                values.extend(part.strip() for part in chunk.split(","))

        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value:
                continue
            compact = " ".join(value.split())
            key = compact.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(compact)
            if len(deduped) >= 200:
                break
        if not deduped:
            raise ValueError("empty_discovery_queries")
        return tuple(deduped)

    def _parse_value(self, key: str, raw: Any) -> Any:
        if key == "forward_target":
            value = str(raw).strip()
            if not value or len(value) > 120:
                raise ValueError("invalid_forward_target")
            return value
        if key == "min_text_length":
            return self._parse_int(raw, 4, 300)
        if key == "per_group_actions_hour":
            return self._parse_int(raw, 0, 1000)
        if key == "per_group_replies_10m":
            return self._parse_int(raw, 0, 30)
        if key == "join_limit_day":
            return self._parse_int(raw, 0, 20)
        if key == "global_actions_minute":
            return self._parse_int(raw, 0, 1000)
        if key == "min_human_delay_sec":
            return self._parse_float(raw, 0.2, 30.0)
        if key == "max_human_delay_sec":
            return self._parse_float(raw, 0.2, 60.0)
        if key == "discovery_enabled":
            return self._parse_bool(raw)
        if key == "discovery_query_limit":
            return self._parse_int(raw, 1, 100)
        if key == "discovery_join_batch":
            return self._parse_int(raw, 1, 30)
        if key == "discovery_queries":
            return self._parse_queries(raw)
        raise ValueError("invalid_config_key")

    @staticmethod
    def _serialize_value(key: str, value: Any) -> str:
        if key == "discovery_queries":
            return ",".join(value)
        if key == "discovery_enabled":
            return "true" if bool(value) else "false"
        return str(value)

    def _build_snapshot(self, current: dict[str, Any]) -> dict[str, Any]:
        min_delay = float(current["min_human_delay_sec"])
        max_delay = float(current["max_human_delay_sec"])
        if max_delay < min_delay:
            raise ValueError("max_delay_must_be_gte_min_delay")

        return {
            "forward_target": str(current["forward_target"]),
            "min_text_length": int(current["min_text_length"]),
            "per_group_actions_hour": int(current["per_group_actions_hour"]),
            "per_group_replies_10m": int(current["per_group_replies_10m"]),
            "join_limit_day": int(current["join_limit_day"]),
            "global_actions_minute": int(current["global_actions_minute"]),
            "min_human_delay_sec": min_delay,
            "max_human_delay_sec": max_delay,
            "discovery_enabled": bool(current["discovery_enabled"]),
            "discovery_query_limit": int(current["discovery_query_limit"]),
            "discovery_join_batch": int(current["discovery_join_batch"]),
            "discovery_queries": tuple(current["discovery_queries"]),
        }
