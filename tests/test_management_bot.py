from app.management_bot import TelegramManagementBot


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
