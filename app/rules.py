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
        self.passenger_announcement_pattern = re.compile(
            r"\b(?:\d+\s*(?:ta\s*)?(?:odam|kishi|passajir)|"
            r"bir\s+kishi|ikki\s+kishi|uch\s+kishi|tort\s+kishi|besh\s+kishi|olti\s+kishi|"
            r"yetti\s+kishi|sakkiz\s+kishi|toqqiz\s+kishi|on\s+kishi)\s+(?:bor|bot|kerak)\b"
        )
        self.bor_people_pattern = re.compile(
            r"\b(?:bor|bot)\s+(?:\d+\s*(?:ta\s*)?)?(?:odam|kishi|passajir)\b"
            r"|\b(?:odam|kishi|passajir)\s+bor\b"
        )
        self.short_order_pattern = re.compile(
            r"\b(?:bor|bot|kerak)\s+(?:\d+\s*(?:ta\s*)?)?(?:odam|kishi|passajir)\b"
            r"|\b(?:\d+\s*(?:ta\s*)?)?(?:odam|kishi|passajir)\s+(?:bor|bot|kerak)\b"
        )
        self.passenger_needed_pattern = re.compile(r"\b\d+\s*(odam|kishi|joy)\s+kerak\b")
        self.route_request_pattern = re.compile(
            r"\b[a-z0-9]{3,}dan\b.*\b(?:yuradigan|ketadigan)\s+kim\s+bor\b"
        )
        self.request_phrase_pattern = re.compile(
            r"\b(?:taxi|taksi|moshin|mashina)\s+kerak\b"
            r"|\b(?:yuradigan|yuradiglar)\s+bormi(?:kan)?\b"
            r"|\bkim\s+bor\b"
            r"|\bolib\s+ketadig(?:an|lar)\s+bormi\b"
        )
        self.offer_context_pattern = re.compile(
            r"\b(?:ketyapman|ketyapmiz|yuryapman|yuryapmiz|olib\s+ketaman|olibketaman|"
            r"olib\s+ketamiz|olibketamiz|zakazga(?:\s+ham)?\s+yuraman|manzildan\s+manzilgach|"
            r"joy\s+bor|bagaj|shafer|shafermiz|haydovchimiz|yuraman|yuramiz|yuryamiz|ketaman|boraman|chiqaman|chiqamiz|komfort)\b"
        )
        self.vehicle_model_pattern = re.compile(
            r"\b(?:kobalt|cobalt|nexia|jentra|malibu|lacetti|damas|spark|captiva|onix|tracker|matiz|epica)\b"
        )

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

        has_short_order = bool(self.short_order_pattern.search(text))
        if len(text) < min_length:
            if (
                not self.route_pattern.search(text)
                and not self.suffix_route_pattern.search(text)
                and not has_short_order
            ):
                return Decision(False, False, reason="too_short")

        tokens = tokenize(text)
        stemmed_tokens = {self._stem_token(token) for token in tokens}
        has_transport = bool(self.transport_pattern.search(text))
        has_request = bool(self.request_pattern.search(text))
        has_offer = bool(self.offer_pattern.search(text))
        has_request_phrase = bool(self.request_phrase_pattern.search(text))
        has_offer_context = bool(self.offer_context_pattern.search(text))
        has_vehicle_model = bool(self.vehicle_model_pattern.search(text))
        has_route = bool(self.route_pattern.search(text)) or bool(self.suffix_route_pattern.search(text))
        region_match = self.geo.detect_region(text)
        has_location = bool(self.location_pattern.search(text)) or bool(stemmed_tokens & self.location_tokens) or bool(
            region_match
        )
        has_exclude = bool(self.exclude_pattern.search(text))
        has_people = bool(self.people_pattern.search(text))
        has_passenger_announcement = bool(self.passenger_announcement_pattern.search(text))
        has_bor_people = bool(self.bor_people_pattern.search(text))
        has_passenger_needed = bool(self.passenger_needed_pattern.search(text))
        has_route_request = bool(self.route_request_pattern.search(text))
        has_yuramiz = "yuramiz" in tokens or "yuryamiz" in tokens

        has_phone = bool(self.phone_pattern.search(raw_text)) or bool(self.phone_pattern.search(text))
        has_username = bool(self.username_pattern.search(raw_text))
        has_contact = has_phone or has_username
        has_order_announcement = (
            (has_route and (has_passenger_announcement or has_bor_people))
            or has_route_request
            or (has_route and has_request_phrase and has_people)
            or has_short_order
        )

        if has_exclude:
            return Decision(False, False, reason="excluded_category")

        if not has_contact and not has_order_announcement:
            return Decision(False, False, reason="no_contact")

        # Offer messages are ignored unless there is explicit request phrase.
        if has_offer and not has_request_phrase:
            return Decision(False, False, reason="taxi_offer")
        if has_yuramiz:
            return Decision(False, False, reason="taxi_offer")
        offer_dominant = (
            has_offer_context
            or has_vehicle_model
            or (has_offer and has_passenger_needed)
            or (has_passenger_needed and has_route and has_transport)
        )
        if offer_dominant and not has_request_phrase:
            return Decision(False, False, reason="taxi_offer")

        score = 0
        if has_request_phrase:
            score += 2
        elif has_request:
            score += 1
        if has_transport:
            score += 1
        if has_route:
            score += 2
        if has_order_announcement:
            score += 2
        if has_location:
            score += 1
        if has_people:
            score += 1
        if has_contact:
            score += 1

        order_signal = has_request_phrase or has_request or has_order_announcement or (has_transport and has_route)
        if has_order_announcement and not offer_dominant:
            return Decision(
                True,
                False,
                reason="taxi_order",
                region_tag=region_match.hashtag if region_match else "#Uzbekiston",
            )
        if score >= 5 and order_signal and not offer_dominant:
            return Decision(
                True,
                False,
                reason="taxi_order",
                region_tag=region_match.hashtag if region_match else "#Uzbekiston",
            )

        return Decision(False, False, reason="no_order_pattern")

    def _sync_dynamic_keywords(self, force: bool = False) -> None:
        if not self.keyword_service:
            if self._keyword_version >= 0 and not force:
                return
            self.transport_tokens = {"taxi", "taksi", "moshin", "mashina", "yandex"}
            self.request_tokens = {"kerak", "bormi", "buyurtma", "zakaz", "yuradigan", "yuradiglar"}
            self.offer_tokens = {
                "boraman",
                "ketaman",
                "yuraman",
                "beraman",
                "taklif",
                "ketyapman",
                "ketyapmiz",
                "olibketaman",
                "olibketamiz",
                "zakazga",
                "joybor",
                "shafermiz",
                "haydovchimiz",
                "chiqaman",
                "chiqamiz",
                "yuramiz",
                "yuryamiz",
            }
            self.location_tokens = {
                "toshkent",
                "toshkint",
                "samarqand",
                "samarqan",
                "jartepa",
                "marhabo",
                "texnagazoil",
                "andijon",
                "namangan",
                "fargona",
                "vodiy",
                "nukus",
                "buxoro",
            }
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
