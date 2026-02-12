from app.text import normalize_text


def test_normalize_handles_emoji_and_whitespace() -> None:
    text = "Taxi   kerak \U0001F695   Toshkentdan   Samarqandga"
    assert normalize_text(text) == "taxi kerak toshkentdan samarqandga"


def test_normalize_handles_cyrillic_and_stylized_text() -> None:
    text = (
        "\uff34\uff21\uff38\uff29"
        " "
        "\u043a\u0435\u0440\u0430\u043a"
        " "
        "\u0422\u043e\u0448\u043a\u0435\u043d\u0442\u0434\u0430\u043d"
        " "
        "\u0421\u0430\u043c\u0430\u0440\u043a\u0430\u043d\u0434\u0433\u0430"
    )
    normalized = normalize_text(text)
    assert "taxi" in normalized
    assert "kerak" in normalized
    assert "toshkentdan" in normalized
