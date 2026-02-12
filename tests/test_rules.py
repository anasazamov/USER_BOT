from app.models import MessageEnvelope, NormalizedMessage
from app.rules import DecisionEngine, RuleConfig
from app.text import normalize_text


def _msg(raw_text: str) -> NormalizedMessage:
    return NormalizedMessage(
        envelope=MessageEnvelope(chat_id=1, message_id=1, sender_id=1, raw_text=raw_text),
        normalized_text=normalize_text(raw_text),
    )


def test_decision_accepts_order_with_phone_contact() -> None:
    engine = DecisionEngine(RuleConfig(min_length=10))
    decision = engine.decide(_msg("Toshkentdan Samarqandga taxi kerak +998901234567"))
    assert decision.should_forward is True
    assert decision.reason == "taxi_order"
    assert decision.region_tag is not None


def test_decision_rejects_order_without_contact() -> None:
    engine = DecisionEngine(RuleConfig(min_length=10))
    decision = engine.decide(_msg("toshkentdan andijonga taxi kerak 2 odam"))
    assert decision.should_forward is False
    assert decision.reason == "no_contact"


def test_decision_rejects_taxi_offer_even_with_contact() -> None:
    engine = DecisionEngine(RuleConfig(min_length=10))
    decision = engine.decide(_msg("toshkentga boraman moshin bor +998901234567"))
    assert decision.should_forward is False
    assert decision.reason == "taxi_offer"


def test_decision_accepts_cyrillic_order_with_username_contact() -> None:
    engine = DecisionEngine(RuleConfig(min_length=10))
    raw = (
        "\u0442\u0430\u043a\u0441\u0438 \u043a\u0435\u0440\u0430\u043a "
        "\u0442\u043e\u0448\u043a\u0435\u043d\u0442\u0434\u0430\u043d "
        "\u043d\u0430\u043c\u0430\u043d\u0433\u0430\u043d\u0433\u0430 @haydovchi_uz"
    )
    decision = engine.decide(_msg(raw))
    assert decision.should_forward is True


def test_decision_rejects_ads() -> None:
    engine = DecisionEngine(RuleConfig(min_length=10))
    decision = engine.decide(_msg("vakansiya reklama va kurs +998901234567"))
    assert decision.should_forward is False
    assert decision.reason == "excluded_category"
