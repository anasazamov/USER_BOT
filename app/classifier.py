"""Trained classifier used as a veto step on regex-approved "order" verdicts.

The classifier is loaded lazily on first predict — if the model file is missing,
`is_available()` stays False and the DecisionEngine simply skips the veto check.
This keeps the userbot functional in environments where no model has been
trained yet, and avoids paying the joblib import cost at startup if classifier
features are not enabled.

Veto semantics: a *low* `predict_order_probability` (e.g. ≤ 0.3) means the model
disagrees strongly with the regex's "order" classification — the message looks
much more like a driver offer or ad. Above the threshold, the regex verdict
stands. This is intentionally conservative: classifier only overrides when it
is confident in the rejection.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_PATH = Path("data/classifier.joblib")
_DEFAULT_VETO_THRESHOLD = 0.3


class TaxiOrderClassifier:
    def __init__(
        self,
        model_path: Path | str | None = None,
        veto_threshold: float | None = None,
        runtime_config: Any = None,
    ) -> None:
        env_path = os.environ.get("CLASSIFIER_MODEL_PATH")
        chosen_path = model_path or env_path or _DEFAULT_MODEL_PATH
        self.model_path = Path(chosen_path)

        env_threshold = os.environ.get("CLASSIFIER_VETO_THRESHOLD")
        chosen_threshold = (
            veto_threshold
            if veto_threshold is not None
            else (float(env_threshold) if env_threshold else _DEFAULT_VETO_THRESHOLD)
        )
        self._fallback_threshold = float(chosen_threshold)
        # When runtime_config is provided, the live snapshot wins each call so admins
        # can retune the veto threshold from the web panel without restarting the bot.
        self.runtime_config = runtime_config

        self._pipeline: Any = None
        self._normalizer = None
        self._load_attempted = False

    @property
    def veto_threshold(self) -> float:
        if self.runtime_config is not None:
            try:
                value = getattr(self.runtime_config.snapshot(), "classifier_veto_threshold", None)
                if value is not None:
                    return float(value)
            except Exception:
                pass
        return self._fallback_threshold

    def _ensure_loaded(self) -> None:
        if self._load_attempted:
            return
        self._load_attempted = True

        if not self.model_path.exists():
            logger.info(
                "classifier_disabled",
                extra={
                    "action": "classifier",
                    "reason": f"model_missing:{self.model_path}",
                },
            )
            return

        try:
            import joblib  # local import — sklearn deps are heavy
            from app.text import normalize_text

            bundle = joblib.load(self.model_path)
            self._pipeline = bundle.get("pipeline") if isinstance(bundle, dict) else bundle
            self._normalizer = normalize_text
            logger.info(
                "classifier_loaded",
                extra={
                    "action": "classifier",
                    "reason": (
                        f"path={self.model_path} "
                        f"veto_threshold={self.veto_threshold} "
                        f"n_train={(bundle.get('n_train_total') if isinstance(bundle, dict) else 'unknown')}"
                    ),
                },
            )
        except Exception as exc:
            logger.warning(
                "classifier_load_failed",
                extra={"action": "classifier", "reason": repr(exc)},
            )
            self._pipeline = None
            self._normalizer = None

    def is_available(self) -> bool:
        self._ensure_loaded()
        return self._pipeline is not None

    def predict_order_probability(self, text: str) -> float | None:
        self._ensure_loaded()
        if self._pipeline is None or not text or not text.strip():
            return None
        try:
            normalized = self._normalizer(text) if self._normalizer else text
            if not normalized.strip():
                return None
            proba = self._pipeline.predict_proba([normalized])[0]
            # Class index 1 = "order" (the positive class set by training script).
            return float(proba[1])
        except Exception as exc:
            logger.warning(
                "classifier_predict_failed",
                extra={"action": "classifier", "reason": repr(exc)},
            )
            return None

    def should_veto_order(self, text: str) -> tuple[bool, float | None]:
        proba = self.predict_order_probability(text)
        if proba is None:
            return False, None
        return proba < self.veto_threshold, proba
