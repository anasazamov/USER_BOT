from datetime import UTC, datetime, timedelta

from app.management_bot import TelegramManagementBot
from app.storage.db import BotSubscriber


def test_parse_command_with_bot_suffix() -> None:
    command, arg = TelegramManagementBot._parse_command("/stats@taxi_bot today")
    assert command == "stats"
    assert arg == "today"


def test_parse_command_without_arg() -> None:
    command, arg = TelegramManagementBot._parse_command("/start")
    assert command == "start"
    assert arg == ""


def test_permanent_subscriber_errors() -> None:
    assert TelegramManagementBot._is_permanent_subscriber_error("Forbidden: bot was blocked by the user")
    assert TelegramManagementBot._is_permanent_subscriber_error("Bad Request: chat not found")
    assert TelegramManagementBot._is_permanent_subscriber_error("USER IS DEACTIVATED")


def test_parse_admin_extend_args_supports_default_and_suffix() -> None:
    user_id, days = TelegramManagementBot._parse_admin_extend_args("12345", default_days=30)
    assert user_id == 12345
    assert days == 30

    user_id, days = TelegramManagementBot._parse_admin_extend_args("12345 45d", default_days=30)
    assert user_id == 12345
    assert days == 45


def test_has_active_access_paid_mode_requires_future_expiry() -> None:
    bot = TelegramManagementBot.__new__(TelegramManagementBot)
    bot.paid_subscription_enabled = True

    future = datetime.now(UTC) + timedelta(days=5)
    subscriber = BotSubscriber(
        user_id=1,
        chat_id=1,
        username="u",
        first_name="Test",
        active=True,
        subscription_status="active",
        subscription_expires_at=str(future),
        subscription_reminder_sent_at=None,
        approved_by_admin_id=None,
        approved_at=None,
        subscribed_at=str(datetime.now(UTC)),
        updated_at=str(datetime.now(UTC)),
    )
    assert TelegramManagementBot._has_active_access(bot, subscriber) is True

    subscriber.subscription_expires_at = str(datetime.now(UTC) - timedelta(minutes=1))
    assert TelegramManagementBot._has_active_access(bot, subscriber) is False
