from __future__ import annotations

import re
from dataclasses import dataclass

from app.geo import GeoResolver
from app.keywords import KeywordService
from app.models import Decision, NormalizedMessage
from app.runtime_config import RuntimeConfigService
from app.text import tokenize


@dataclass(slots=True)
class RuleConfig:
    min_length: int


class DecisionEngine:
    """Order-only classifier: finds taxi requests, rejects taxi offers."""

    def __init__(
        self,
        config: RuleConfig,
        keyword_service: KeywordService | None = None,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self.config = config
        self.geo = GeoResolver()
        self.keyword_service = keyword_service
        self.runtime_config = runtime_config
        self._keyword_version = -1

        self.transport_tokens: set[str] = set()
        self.request_tokens: set[str] = set()
        self.offer_tokens: set[str] = set()
        self.location_tokens: set[str] = set()
        self.route_tokens: set[str] = set()
        self.exclude_tokens: set[str] = set()

        self.transport_pattern = re.compile(r"$^")
        self.request_pattern = re.compile(r"$^")
        self.offer_pattern = re.compile(r"$^")
        self.location_pattern = re.compile(r"$^")
        self.exclude_pattern = re.compile(r"$^")

        self.route_pattern = re.compile(
            r"\b([a-z0-9]{3,})\s+(dan|from)\s+([a-z0-9]{2,})\b|\b([a-z0-9]{3,})\s+(ga|to)\s+([a-z0-9]{2,})\b"
        )
        self.suffix_route_pattern = re.compile(r"\b[a-z0-9]{3,}dan\b.*\b[a-z0-9]{2,}ga\b")
        self.people_pattern = re.compile(r"\b\d+\s*(odam|kishi|passajir|joy)\b")

        # Contact is mandatory to pass final decision.
        self.phone_pattern = re.compile(r"(?<!\d)(?:\+?998)?[\s\-()]*(?:\d[\s\-()]*){7,12}(?!\d)")
        self.username_pattern = re.compile(r"(?<!\w)@[A-Za-z][A-Za-z0-9_]{4,}")

        self._sync_dynamic_keywords(force=True)

    def decide(self, message: NormalizedMessage) -> Decision:
        self._sync_dynamic_keywords()

        text = message.normalized_text
        raw_text = message.envelope.raw_text or ""
        min_length = self.runtime_config.snapshot().min_text_length if self.runtime_config else self.config.min_length
        if not text:
            return Decision(False, False, reason="empty_text")

        if len(text) < min_length:
            if not self.route_pattern.search(text) and not self.suffix_route_pattern.search(text):
                return Decision(False, False, reason="too_short")

        tokens = tokenize(text)
        stemmed_tokens = {self._stem_token(token) for token in tokens}
        has_transport = bool(self.transport_pattern.search(text))
        has_request = bool(self.request_pattern.search(text))
        has_offer = bool(self.offer_pattern.search(text))
        has_route = bool(self.route_pattern.search(text)) or bool(self.suffix_route_pattern.search(text))
        region_match = self.geo.detect_region(text)
        has_location = bool(self.location_pattern.search(text)) or bool(stemmed_tokens & self.location_tokens) or bool(
            region_match
        )
        has_exclude = bool(self.exclude_pattern.search(text))
        has_people = bool(self.people_pattern.search(text))

        has_phone = bool(self.phone_pattern.search(raw_text)) or bool(self.phone_pattern.search(text))
        has_username = bool(self.username_pattern.search(raw_text))
        has_contact = has_phone or has_username

        if has_exclude:
            return Decision(False, False, reason="excluded_category")

        if not has_contact:
            return Decision(False, False, reason="no_contact")

        # Offer messages are ignored unless there is explicit request signal.
        if has_offer and not has_request:
            return Decision(False, False, reason="taxi_offer")

        score = 0
        if has_request:
            score += 2
        if has_transport:
            score += 1
        if has_route:
            score += 2
        if has_location:
            score += 1
        if has_people:
            score += 1
        if has_contact:
            score += 2

        if score >= 5 and (has_request or has_route):
            reply = "Buyurtma qabul qilindi."
            return Decision(
                True,
                True,
                reply_text=reply,
                reason="taxi_order",
                region_tag=region_match.hashtag if region_match else "#Uzbekiston",
            )

        return Decision(False, False, reason="no_order_pattern")

    def _sync_dynamic_keywords(self, force: bool = False) -> None:
        if not self.keyword_service:
            if self._keyword_version >= 0 and not force:
                return
            self.transport_tokens = {"taxi", "taksi", "moshin", "mashina", "yandex"}
            self.request_tokens = {"kerak", "bormi", "buyurtma", "zakaz", "yuradigan"}
            self.offer_tokens = {"boraman", "ketaman", "yuraman", "beraman", "taklif"}
            self.location_tokens = {"toshkent", "samarqand", "andijon", "namangan", "fargona", "nukus", "buxoro"}
            self.route_tokens = {"dan", "ga", "from", "to"}
            self.exclude_tokens = {"vakansiya", "reklama", "kurs", "kanal", "job"}
            self._rebuild_patterns()
            self._keyword_version = 0
            return

        snapshot = self.keyword_service.snapshot()
        if not force and snapshot.version == self._keyword_version:
            return

        self.transport_tokens = set(snapshot.transport)
        self.request_tokens = set(snapshot.request)
        self.offer_tokens = set(snapshot.offer)
        self.location_tokens = set(snapshot.location)
        self.route_tokens = set(snapshot.route)
        self.exclude_tokens = set(snapshot.exclude)
        self._rebuild_patterns()
        self._keyword_version = snapshot.version

    def _rebuild_patterns(self) -> None:
        self.transport_pattern = self._compile_token_pattern(self.transport_tokens)
        self.request_pattern = self._compile_token_pattern(self.request_tokens)
        self.offer_pattern = self._compile_token_pattern(self.offer_tokens)
        self.location_pattern = self._compile_token_pattern(self.location_tokens)
        self.exclude_pattern = self._compile_token_pattern(self.exclude_tokens)

    @staticmethod
    def _compile_token_pattern(tokens: set[str]) -> re.Pattern[str]:
        if not tokens:
            return re.compile(r"$^")
        escaped = "|".join(sorted(re.escape(token) for token in tokens))
        return re.compile(rf"\b({escaped})\b")

    @staticmethod
    def _stem_token(token: str) -> str:
        for suffix in ("lardan", "dan", "ga", "ni", "da"):
            if token.endswith(suffix) and len(token) > len(suffix) + 2:
                return token[: -len(suffix)]
        return token
