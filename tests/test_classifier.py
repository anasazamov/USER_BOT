from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.classifier import TaxiOrderClassifier
from app.models import MessageEnvelope, NormalizedMessage
from app.rules import DecisionEngine, RuleConfig
from app.text import normalize_text


def _msg(raw_text: str) -> NormalizedMessage:
    return NormalizedMessage(
        envelope=MessageEnvelope(chat_id=1, message_id=1, sender_id=1, raw_text=raw_text),
        normalized_text=normalize_text(raw_text),
    )


def test_classifier_unavailable_when_model_missing(tmp_path: Path) -> None:
    clf = TaxiOrderClassifier(model_path=tmp_path / "nope.joblib")
    assert not clf.is_available()
    assert clf.predict_order_probability("anything") is None
    veto, proba = clf.should_veto_order("anything")
    assert veto is False
    assert proba is None


def test_classifier_empty_text_returns_none(tmp_path: Path) -> None:
    clf = TaxiOrderClassifier(model_path=tmp_path / "nope.joblib")
    assert clf.predict_order_probability("") is None
    assert clf.predict_order_probability("   ") is None


def test_classifier_veto_threshold_from_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLASSIFIER_VETO_THRESHOLD", "0.45")
    clf = TaxiOrderClassifier(model_path=tmp_path / "missing.joblib")
    assert clf.veto_threshold == 0.45


def test_classifier_veto_threshold_arg_overrides_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLASSIFIER_VETO_THRESHOLD", "0.45")
    clf = TaxiOrderClassifier(model_path=tmp_path / "missing.joblib", veto_threshold=0.2)
    assert clf.veto_threshold == 0.2


class _StubClassifier:
    """In-memory classifier double — bypasses joblib loading."""

    def __init__(self, proba: float, threshold: float = 0.3) -> None:
        self._proba = proba
        self.veto_threshold = threshold

    def is_available(self) -> bool:
        return True

    def predict_order_probability(self, text: str) -> float:
        return self._proba

    def should_veto_order(self, text: str) -> tuple[bool, float]:
        return self._proba < self.veto_threshold, self._proba


def test_decision_engine_drops_when_no_classifier() -> None:
    # New pipeline is model-only — no classifier means no forward at all.
    engine = DecisionEngine(RuleConfig(min_length=10))
    decision = engine.decide(_msg("Toshkentdan Samarqandga taxi kerak +998901234567"))
    assert decision.should_forward is False
    assert decision.reason == "classifier_unavailable"


def test_decision_engine_forwards_when_classifier_says_order() -> None:
    engine = DecisionEngine(
        RuleConfig(min_length=10),
        classifier=_StubClassifier(proba=0.85),
    )
    decision = engine.decide(_msg("Toshkentdan Samarqandga taxi kerak +998901234567"))
    assert decision.should_forward is True
    assert decision.reason.startswith("model_order:")


def test_decision_engine_drops_when_classifier_says_not_order() -> None:
    engine = DecisionEngine(
        RuleConfig(min_length=10),
        classifier=_StubClassifier(proba=0.15),
    )
    decision = engine.decide(_msg("Toshkentdan Samarqandga taxi kerak +998901234567"))
    assert decision.should_forward is False
    assert decision.reason.startswith("model_not_order:")


def test_classifier_uses_runtime_config_threshold_dynamically(tmp_path: Path) -> None:
    """The veto threshold should be re-read from runtime_config on every call,
    so admin web edits take effect without restarting the classifier."""

    class _MutableRuntime:
        def __init__(self, threshold: float) -> None:
            self._threshold = threshold

        def snapshot(self):
            return SimpleNamespace(classifier_veto_threshold=self._threshold)

        def set_threshold(self, value: float) -> None:
            self._threshold = value

    runtime = _MutableRuntime(0.5)
    clf = TaxiOrderClassifier(
        model_path=tmp_path / "missing.joblib",
        veto_threshold=0.99,  # would normally win, but runtime overrides
        runtime_config=runtime,
    )
    assert clf.veto_threshold == 0.5
    runtime.set_threshold(0.25)
    assert clf.veto_threshold == 0.25


