import asyncio

from app.keywords import KeywordService
from app.storage.db import KEYWORD_KINDS


class _FakeRepo:
    def __init__(self) -> None:
        self.store: dict[str, set[str]] = {kind: set() for kind in KEYWORD_KINDS}

    async def ensure_default_keyword_rules(self, defaults: dict[str, set[str]]) -> None:
        if any(self.store.values()):
            return
        for kind, values in defaults.items():
            if kind in self.store:
                self.store[kind].update(values)

    async def fetch_keyword_rules(self) -> dict[str, set[str]]:
        return {kind: set(values) for kind, values in self.store.items()}

    async def upsert_keyword_rule(self, kind: str, value: str) -> None:
        self.store[kind].add(value)

    async def delete_keyword_rule(self, kind: str, value: str) -> bool:
        if value in self.store[kind]:
            self.store[kind].remove(value)
            return True
        return False


def test_keyword_service_add_delete_with_cyrillic() -> None:
    repo = _FakeRepo()
    service = KeywordService(repo)  # type: ignore[arg-type]

    async def run() -> None:
        await service.initialize()
        added = await service.add_keyword("transport", "\u043c\u0430\u0448\u0438\u043d\u0430")
        assert "mashina" in added
        snapshot = service.snapshot()
        assert "mashina" in snapshot.transport

        deleted = await service.delete_keyword("transport", "\u043c\u0430\u0448\u0438\u043d\u0430")
        assert "mashina" in deleted

    asyncio.run(run())

