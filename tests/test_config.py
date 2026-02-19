from app.config import Settings


def test_settings_realtime_only_default_true(monkeypatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.delenv("REALTIME_ONLY", raising=False)

    settings = Settings.from_env()
    assert settings.realtime_only is True


def test_settings_realtime_only_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("REALTIME_ONLY", "false")

    settings = Settings.from_env()
    assert settings.realtime_only is False


def test_settings_bot_admin_ids_fallback_to_owner(monkeypatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("OWNER_USER_ID", "999")
    monkeypatch.delenv("BOT_ADMIN_USER_IDS", raising=False)

    settings = Settings.from_env()
    assert settings.bot_admin_user_ids == (999,)


def test_settings_bot_admin_ids_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("BOT_ADMIN_USER_IDS", "1,2,3")

    settings = Settings.from_env()
    assert settings.bot_admin_user_ids == (1, 2, 3)
