from app.actions import ActionExecutor


def test_publish_message_contains_source_and_region() -> None:
    message = ActionExecutor.format_publish_message(
        raw_text="Toshkentdan Andijonga taxi kerak 2 odam",
        source_link="https://t.me/testgroup/123",
        region_tag="#AndijonViloyati",
    )
    assert "Taxi buyurtma" in message
    assert "#AndijonViloyati" in message
    assert "Status: Yangi" in message
    assert "https://t.me/testgroup/123" in message


def test_publish_message_fallback_source() -> None:
    message = ActionExecutor.format_publish_message(
        raw_text="Namanganga yuradigan moshin bormi",
        source_link="",
        region_tag=None,
    )
    assert "#Uzbekiston" in message
    assert "Status: Yangi" in message
    assert "Manba: private chat" in message


def test_resolve_forward_target_numeric_chat_id() -> None:
    assert ActionExecutor._resolve_forward_target("-1001234567890") == -1001234567890


def test_resolve_forward_target_username() -> None:
    assert ActionExecutor._resolve_forward_target("@taxi_orders_uz") == "@taxi_orders_uz"


def test_publish_message_custom_status() -> None:
    message = ActionExecutor.format_publish_message(
        raw_text="Jartepadan shaharga 1 kishi bor",
        source_link="https://t.me/testgroup/444",
        region_tag="#SamarqandViloyati",
        status_label="Yangilandi",
    )
    assert "Status: Yangilandi" in message
