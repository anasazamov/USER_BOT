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
