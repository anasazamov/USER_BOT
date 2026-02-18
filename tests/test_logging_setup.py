import json
import logging

from app.logging_setup import JsonFormatter


def test_json_formatter_includes_extended_message_context() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="message_filtered",
        args=(),
        exc_info=None,
    )
    record.chat_id = -100123456
    record.message_id = 777
    record.chat_title = "My Group"
    record.chat_username = "@mygroup"
    record.chat_ref = "@mygroup"
    record.raw_preview = "raw text"
    record.normalized_preview = "normalized text"
    record.action = "filter_drop"
    record.reason = "likely_taxi_offer"

    payload = json.loads(JsonFormatter().format(record))
    assert payload["chat_id"] == -100123456
    assert payload["message_id"] == 777
    assert payload["chat_title"] == "My Group"
    assert payload["chat_username"] == "@mygroup"
    assert payload["chat_ref"] == "@mygroup"
    assert payload["raw_preview"] == "raw text"
    assert payload["normalized_preview"] == "normalized text"
