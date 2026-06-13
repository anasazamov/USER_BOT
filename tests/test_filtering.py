from app.filtering import FastFilter
from app.text import normalize_text


def test_fast_filter_rejects_empty() -> None:
    engine = FastFilter(min_length=10)
    result = engine.evaluate("")
    assert result.passed is False
    assert result.reason == "empty_text"


def test_fast_filter_rejects_too_short() -> None:
    engine = FastFilter(min_length=10)
    result = engine.evaluate("ok")
    assert result.passed is False
    assert result.reason == "too_short"


def test_fast_filter_passes_anything_else_to_model() -> None:
    engine = FastFilter(min_length=10)
    # The model-only pipeline deliberately does not pre-filter on offer or ad
    # keywords — that's now the classifier's job. The fast filter only drops
    # obvious empties and too-short messages.
    samples = [
        "toshkentdan samarqandga taxi kerak",
        "toshkentga boraman moshin bor",     # would have been "offer" under old regex
        "vakansiya reklama xizmat kurs",      # would have been "ad" under old regex
        "tqxi kerakk toshkentdan andijonga 2 odam",
    ]
    for text in samples:
        result = engine.evaluate(normalize_text(text))
        assert result.passed is True, text
        assert result.reason == "pass_to_model"
