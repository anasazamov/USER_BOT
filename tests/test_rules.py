from types import SimpleNamespace

from app.models import MessageEnvelope, NormalizedMessage
from app.rules import DecisionEngine, RuleConfig
from app.text import normalize_text


def _msg(raw_text: str) -> NormalizedMessage:
    return NormalizedMessage(
        envelope=MessageEnvelope(chat_id=1, message_id=1, sender_id=1, raw_text=raw_text),
        normalized_text=normalize_text(raw_text),
    )


class _StubClassifier:
    """In-memory classifier double used to verify decision logic without loading
    the real joblib model."""

    def __init__(self, proba: float, threshold: float = 0.5, available: bool = True) -> None:
        self._proba = proba
        self._available = available
        self.veto_threshold = threshold

    def is_available(self) -> bool:
        return self._available

    def predict_order_probability(self, text: str) -> float | None:
        return self._proba if self._available else None


class _RuntimeWithThreshold:
    def __init__(self, threshold: float) -> None:
        self._threshold = threshold

    def snapshot(self):
        return SimpleNamespace(classifier_veto_threshold=self._threshold)


def test_decision_drops_when_no_classifier() -> None:
    # Without a classifier we deliberately refuse to forward — the new pipeline
    # is model-only, so a missing model file must not silently fall back to
    # regex behaviour or to "forward everything".
    engine = DecisionEngine(RuleConfig(min_length=10))
    decision = engine.decide(_msg("toshkentdan samarqandga taxi kerak +998901234567"))
    assert decision.should_forward is False
    assert decision.reason == "classifier_unavailable"


def test_decision_drops_when_classifier_unavailable() -> None:
    engine = DecisionEngine(
        RuleConfig(min_length=10),
        classifier=_StubClassifier(proba=0.99, available=False),
    )
    decision = engine.decide(_msg("toshkentdan samarqandga taxi kerak +998901234567"))
    assert decision.should_forward is False
    assert decision.reason == "classifier_unavailable"


def test_decision_drops_empty_text() -> None:
    engine = DecisionEngine(
        RuleConfig(min_length=10),
        classifier=_StubClassifier(proba=0.99),
    )
    decision = engine.decide(_msg(""))
    assert decision.should_forward is False
    assert decision.reason == "empty_text"


def test_decision_drops_too_short() -> None:
    engine = DecisionEngine(
        RuleConfig(min_length=10),
        classifier=_StubClassifier(proba=0.99),
    )
    decision = engine.decide(_msg("ok"))
    assert decision.should_forward is False
    assert decision.reason == "too_short"


def test_decision_forwards_when_proba_above_threshold() -> None:
    engine = DecisionEngine(
        RuleConfig(min_length=10),
        runtime_config=_RuntimeWithThreshold(0.5),
        classifier=_StubClassifier(proba=0.85, threshold=0.5),
    )
    decision = engine.decide(_msg("Toshkentdan Samarqandga taxi kerak +998901234567"))
    assert decision.should_forward is True
    assert decision.reason.startswith("model_order:")
    assert decision.region_tag is not None  # region detected from text


def test_decision_drops_when_proba_below_threshold() -> None:
    engine = DecisionEngine(
        RuleConfig(min_length=10),
        runtime_config=_RuntimeWithThreshold(0.5),
        classifier=_StubClassifier(proba=0.20, threshold=0.5),
    )
    decision = engine.decide(_msg("toshkentga boraman moshin bor +998901234567"))
    assert decision.should_forward is False
    assert decision.reason.startswith("model_not_order:")


def test_decision_strict_less_than_threshold_boundary() -> None:
    # At exactly the threshold the message is accepted (>=, not >).
    engine = DecisionEngine(
        RuleConfig(min_length=10),
        runtime_config=_RuntimeWithThreshold(0.5),
        classifier=_StubClassifier(proba=0.5, threshold=0.5),
    )
    decision = engine.decide(_msg("Toshkentdan Samarqandga taxi kerak +998901234567"))
    assert decision.should_forward is True


def test_decision_threshold_default_when_no_runtime_config() -> None:
    # Without runtime_config the default accept threshold is 0.5.
    engine = DecisionEngine(
        RuleConfig(min_length=10),
        classifier=_StubClassifier(proba=0.49, threshold=0.5),
    )
    decision = engine.decide(_msg("Toshkentdan Samarqandga taxi kerak +998901234567"))
    assert decision.should_forward is False
    assert decision.reason.startswith("model_not_order:")
