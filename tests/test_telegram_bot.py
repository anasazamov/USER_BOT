from app.telegram_bot import TelegramUserbot


class _Msg:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class _Dialog:
    def __init__(self, message_id: int | None = None, top_message: int | None = None) -> None:
        self.message = _Msg(message_id) if message_id is not None else None
        self.top_message = top_message


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
