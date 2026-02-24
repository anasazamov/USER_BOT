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


def test_settings_paid_subscription_options(monkeypatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("BOT_PAID_SUBSCRIPTION_ENABLED", "true")
    monkeypatch.setenv("BOT_SUBSCRIPTION_DEFAULT_DAYS", "45")
    monkeypatch.setenv("BOT_SUBSCRIPTION_REMINDER_HOURS", "24")
    monkeypatch.setenv("BOT_SUBSCRIPTION_CHECK_INTERVAL_SEC", "120")
    monkeypatch.setenv("BOT_MANAGED_PRIVATE_GROUP_IDS", "-1001,-1002")

    settings = Settings.from_env()
    assert settings.bot_paid_subscription_enabled is True
    assert settings.bot_subscription_default_days == 45
    assert settings.bot_subscription_reminder_hours == 24
    assert settings.bot_subscription_check_interval_sec == 120
    assert settings.bot_managed_private_group_ids == (-1001, -1002)


def test_settings_telegram_read_ack_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.delenv("TELEGRAM_READ_ACK_ENABLED", raising=False)

    settings = Settings.from_env()
    assert settings.telegram_read_ack_enabled is False


def test_settings_telegram_read_ack_can_be_enabled(monkeypatch) -> None:
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_READ_ACK_ENABLED", "true")

    settings = Settings.from_env()
    assert settings.telegram_read_ack_enabled is True
