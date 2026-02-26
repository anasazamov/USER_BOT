import asyncio

from app.config import Settings
from app.runtime_config import CONFIG_KEYS, RuntimeConfigService


class _Repo:
    def __init__(self) -> None:
        self.saved: dict[str, str] = {}

    async def fetch_runtime_config(self) -> dict[str, str]:
        return {"forward_target": "@stale_target", "global_actions_minute": "999"}

    async def upsert_runtime_config(self, key: str, value: str) -> None:
        self.saved[key] = value


def test_runtime_config_sync_from_settings_persists_env_values() -> None:
    async def run() -> None:
        repo = _Repo()
        settings = Settings(
            api_id=1,
            api_hash="hash",
            forward_target="-1003342262169",
            min_text_length=5,
            per_group_actions_hour=0,
            per_group_replies_10m=0,
            join_limit_day=20,
            global_actions_minute=0,
            min_human_delay_sec=0.3,
            max_human_delay_sec=0.9,
            discovery_enabled=False,
            discovery_query_limit=20,
            discovery_join_batch=4,
            discovery_queries=("taxi tashkent", "taxi samarqand"),
        )
        service = RuntimeConfigService(settings=settings, repository=repo)  # type: ignore[arg-type]

        snapshot = await service.sync_from_settings()

        assert snapshot.forward_target == "-1003342262169"
        assert snapshot.global_actions_minute == 0
        assert snapshot.discovery_enabled is False
        assert repo.saved["forward_target"] == "-1003342262169"
        assert repo.saved["global_actions_minute"] == "0"
        assert repo.saved["discovery_enabled"] == "false"
        assert repo.saved["discovery_queries"] == "taxi tashkent,taxi samarqand"
        assert set(repo.saved) == set(CONFIG_KEYS)

    asyncio.run(run())

