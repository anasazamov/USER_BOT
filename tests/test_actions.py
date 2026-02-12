from app.actions import ActionExecutor


def test_publish_message_contains_source_and_region() -> None:
    message = ActionExecutor.format_publish_message(
        raw_text="Toshkentdan Andijonga taxi kerak 2 odam",
        source_link="https://t.me/testgroup/123",
        region_tag="#AndijonViloyati",
    )
    assert "Taxi buyurtma" in message
    assert "#AndijonViloyati" in message
    assert "https://t.me/testgroup/123" in message


def test_publish_message_fallback_source() -> None:
    message = ActionExecutor.format_publish_message(
        raw_text="Namanganga yuradigan moshin bormi",
        source_link="",
        region_tag=None,
    )
    assert "#Uzbekiston" in message
    assert "Manba: private chat" in message

