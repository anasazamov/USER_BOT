from app.telegram_bot import TelegramUserbot


class _Msg:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class _Dialog:
    def __init__(self, message_id: int | None = None, top_message: int | None = None) -> None:
        self.message = _Msg(message_id) if message_id is not None else None
        self.top_message = top_message


class _DialogType:
    def __init__(self, is_group: bool, is_channel: bool) -> None:
        self.is_group = is_group
        self.is_channel = is_channel


def test_dialog_latest_message_id_prefers_message_object() -> None:
    dialog = _Dialog(message_id=345, top_message=777)
    assert TelegramUserbot._dialog_latest_message_id(dialog) == 345


def test_dialog_latest_message_id_falls_back_to_top_message() -> None:
    dialog = _Dialog(message_id=None, top_message=888)
    assert TelegramUserbot._dialog_latest_message_id(dialog) == 888


def test_dialog_latest_message_id_returns_zero_without_ids() -> None:
    dialog = _Dialog(message_id=None, top_message=None)
    assert TelegramUserbot._dialog_latest_message_id(dialog) == 0


def test_build_message_log_context_contains_group_and_preview() -> None:
    context = TelegramUserbot._build_message_log_context(
        chat_id=-1001,
        chat_username="my_group",
        chat_title="My Group",
        raw_text="Samarqanddan toshkentga bor odam +998901234567",
        normalized_text="samarqanddan toshkentga bor odam 998901234567",
    )
    assert context["chat_ref"] == "@my_group"
    assert context["chat_title"] == "My Group"
    assert context["chat_username"] == "@my_group"
    assert "raw_preview" in context
    assert "normalized_preview" in context


def test_summarize_dialogs_counts_monitored_chats() -> None:
    dialogs = [
        _DialogType(is_group=True, is_channel=False),
        _DialogType(is_group=False, is_channel=True),
        _DialogType(is_group=False, is_channel=False),
    ]
    summary = TelegramUserbot._summarize_dialogs(dialogs)
    assert summary["total"] == 3
    assert summary["groups"] == 1
    assert summary["channels"] == 1
    assert summary["private"] == 1
    assert summary["monitored_chats"] == 2


def test_is_target_match_by_username() -> None:
    assert TelegramUserbot._is_target_match("@test_taxi_order", -100777, "test_taxi_order") is True
    assert TelegramUserbot._is_target_match("@test_taxi_order", -100777, "another_group") is False


def test_is_target_match_by_numeric_id() -> None:
    assert TelegramUserbot._is_target_match("-1001234567890", -1001234567890, None) is True
    assert TelegramUserbot._is_target_match("-1001234567890", -1000000000000, None) is False


def test_is_target_match_ignores_me_targets() -> None:
    assert TelegramUserbot._is_target_match("me", -100777, "test_taxi_order") is False
