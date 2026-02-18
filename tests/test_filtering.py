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


def test_fast_filter_rejects_driver_offer_with_people_needed() -> None:
    engine = FastFilter(min_length=10)
    text = normalize_text(
        "\u043f\u0438\u0442\u0435\u0440\u0434\u0430\u043d "
        "\u0445\u043e\u0440\u0430\u0437\u043c\u0433\u0430 "
        "\u043c\u043e\u0448\u0438\u043d\u0434\u0430 \u043a\u0435\u0442\u044f\u043f\u043c\u0430\u043d "
        "2 \u043e\u0434\u0430\u043c \u043a\u0435\u0440\u0430\u043a +998901234567"
    )
    result = engine.evaluate(text)
    assert result.passed is False
    assert result.reason == "likely_taxi_offer"


def test_fast_filter_accepts_passenger_style_route_orders() -> None:
    engine = FastFilter(min_length=10)
    samples = [
        "Shahardan jartepaga 1kishi bor +998121234567",
        "Jartepadan shaxarga 1 kishi bor +99812365478",
        "Shaxardan jartepaga 1 ta odam bor",
        "991234567 Shahardan jartepaga 1 kishi bot",
        "Ertalab 6 ga jartepadan Shaharga 2 kishi bor +998561234567",
        "Ertaga ertalabga 7:30 da marhobodan jartepaga bir kishi bor",
    ]
    for raw in samples:
        result = engine.evaluate(normalize_text(raw))
        assert result.passed is True, raw


def test_fast_filter_accepts_route_request_with_pochta() -> None:
    engine = FastFilter(min_length=10)
    result = engine.evaluate(normalize_text("Shahardan yuradigan kim bor pochta bor"))
    assert result.passed is True
