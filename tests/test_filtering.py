from app.filtering import FastFilter
from app.text import normalize_text


def test_fast_filter_accepts_typo_order_message() -> None:
    engine = FastFilter(min_length=10)
    text = normalize_text("tqxi kerakk toshkentdan andijonga 2 odam")
    result = engine.evaluate(text)
    assert result.passed is True


def test_fast_filter_rejects_ads() -> None:
    engine = FastFilter(min_length=10)
    result = engine.evaluate(normalize_text("vakansiya reklama xizmat kurs"))
    assert result.passed is False
    assert result.reason == "exclude_keyword"


def test_fast_filter_rejects_offer_message() -> None:
    engine = FastFilter(min_length=10)
    result = engine.evaluate(normalize_text("toshkentga boraman moshin bor"))
    assert result.passed is False
    assert result.reason == "likely_taxi_offer"


def test_fast_filter_accepts_request_phrase() -> None:
    engine = FastFilter(min_length=10)
    result = engine.evaluate(normalize_text("andijonga yuradiglar bormi moshin kerak"))
    assert result.passed is True


def test_fast_filter_accepts_cyrillic_order() -> None:
    engine = FastFilter(min_length=10)
    text = normalize_text(
        "\u0442\u0430\u043a\u0441\u0438 \u043a\u0435\u0440\u0430\u043a "
        "\u0442\u043e\u0448\u043a\u0435\u043d\u0442\u0434\u0430\u043d "
        "\u0430\u043d\u0434\u0438\u0436\u043e\u043d\u0433\u0430 2 \u043e\u0434\u0430\u043c"
    )
    result = engine.evaluate(text)
    assert result.passed is True
