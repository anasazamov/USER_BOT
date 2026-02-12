from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.storage.db import ActionRepository, KEYWORD_KINDS
from app.text import normalize_text, tokenize

DEFAULT_KEYWORDS: dict[str, set[str]] = {
    "transport": {
        "taxi",
        "taksi",
        "yandex",
        "mytaxi",
        "zakaz",
        "buyurtma",
        "haydovchi",
        "driver",
        "moshin",
        "mashin",
        "mashina",
        "avto",
        "ulov",
    },
    "request": {
        "kerak",
        "kere",
        "bormi",
        "bormikan",
        "qayerga",
        "buyurtma",
        "zakaz",
        "yuradigan",
        "yuradiglar",
        "ketadigan",
        "olibketadigan",
        "keremi",
    },
    "offer": {
        "boraman",
        "ketaman",
        "yuraman",
        "olibketaman",
        "ketyapman",
        "ketyapmiz",
        "yuryapman",
        "yuryapmiz",
        "olibketamiz",
        "chiqaman",
        "chiqamiz",
        "zakazga",
        "joybor",
        "bagaj",
        "pochta",
        "shafer",
        "shafermiz",
        "haydovchimiz",
        "beraman",
        "xizmat",
        "taklif",
        "bosh",
    },
    "exclude": {
        "vakansiya",
        "reklama",
        "dostavka",
        "kredit",
        "obuna",
        "kanal",
        "kurs",
        "sotiladi",
        "sotaman",
        "ishga",
        "job",
        "marketing",
    },
    "location": {
        "toshkent",
        "tashkent",
        "samarqand",
        "samarkand",
        "andijon",
        "namangan",
        "fargona",
        "fergana",
        "nukus",
        "buxoro",
        "jizzax",
        "xorazm",
        "urganch",
        "termiz",
        "qarshi",
        "navoiy",
        "guliston",
    },
    "route": {"dan", "ga", "from", "to"},
}


@dataclass(frozen=True, slots=True)
class KeywordSnapshot:
    transport: frozenset[str]
    request: frozenset[str]
    offer: frozenset[str]
    exclude: frozenset[str]
    location: frozenset[str]
    route: frozenset[str]
    version: int = 0


class KeywordService:
    def __init__(self, repository: ActionRepository) -> None:
        self.repository = repository
        self._snapshot = KeywordSnapshot(
            transport=frozenset(DEFAULT_KEYWORDS["transport"]),
            request=frozenset(DEFAULT_KEYWORDS["request"]),
            offer=frozenset(DEFAULT_KEYWORDS["offer"]),
            exclude=frozenset(DEFAULT_KEYWORDS["exclude"]),
            location=frozenset(DEFAULT_KEYWORDS["location"]),
            route=frozenset(DEFAULT_KEYWORDS["route"]),
            version=0,
        )
        self._lock = asyncio.Lock()
        self._version = 0

    async def initialize(self) -> None:
        await self.repository.ensure_default_keyword_rules(DEFAULT_KEYWORDS)
        await self.reload()

    def snapshot(self) -> KeywordSnapshot:
        return self._snapshot

    async def reload(self) -> KeywordSnapshot:
        async with self._lock:
            grouped = await self.repository.fetch_keyword_rules()
            self._version += 1
            self._snapshot = KeywordSnapshot(
                transport=frozenset(grouped["transport"]),
                request=frozenset(grouped["request"]),
                offer=frozenset(grouped["offer"]),
                exclude=frozenset(grouped["exclude"]),
                location=frozenset(grouped["location"]),
                route=frozenset(grouped["route"]),
                version=self._version,
            )
            return self._snapshot

    async def add_keyword(self, kind: str, value: str) -> list[str]:
        if kind not in KEYWORD_KINDS:
            raise ValueError("invalid_keyword_kind")

        normalized = normalize_text(value)
        tokens = [token for token in tokenize(normalized) if token]
        if not tokens:
            return []

        for token in tokens:
            await self.repository.upsert_keyword_rule(kind, token)
        await self.reload()
        return tokens

    async def delete_keyword(self, kind: str, value: str) -> list[str]:
        if kind not in KEYWORD_KINDS:
            raise ValueError("invalid_keyword_kind")

        normalized = normalize_text(value)
        tokens = [token for token in tokenize(normalized) if token]
        deleted: list[str] = []
        for token in tokens:
            removed = await self.repository.delete_keyword_rule(kind, token)
            if removed:
                deleted.append(token)
        await self.reload()
        return deleted

    async def list_keywords(self) -> dict[str, list[str]]:
        snapshot = self.snapshot()
        return {
            "transport": sorted(snapshot.transport),
            "request": sorted(snapshot.request),
            "offer": sorted(snapshot.offer),
            "exclude": sorted(snapshot.exclude),
            "location": sorted(snapshot.location),
            "route": sorted(snapshot.route),
        }
