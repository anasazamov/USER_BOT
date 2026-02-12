from __future__ import annotations

import re
from dataclasses import dataclass

from app.geo import GeoResolver
from app.keywords import KeywordService
from app.runtime_config import RuntimeConfigService
from app.text import tokenize


@dataclass(slots=True)
class FastFilterResult:
    passed: bool
    reason: str
    score: int = 0


class FastFilter:
    def __init__(
        self,
        min_length: int,
        keyword_service: KeywordService | None = None,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self.min_length = min_length
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

        self._transport_by_len: dict[int, tuple[str, ...]] = {}
        self._request_by_len: dict[int, tuple[str, ...]] = {}
        self._location_by_len: dict[int, tuple[str, ...]] = {}
        self._offer_by_len: dict[int, tuple[str, ...]] = {}

        self.route_pattern = re.compile(
            r"\b([a-z0-9]{3,})\s+(dan|from)\s+([a-z0-9]{2,})\b|\b([a-z0-9]{3,})\s+(ga|to)\s+([a-z0-9]{2,})\b"
        )
        self.suffix_route_pattern = re.compile(r"\b[a-z0-9]{3,}dan\b.*\b[a-z0-9]{2,}ga\b")
        self.people_pattern = re.compile(r"\b\d+\s*(odam|kishi|passajir|joy)\b")
        self.passenger_needed_pattern = re.compile(r"\b\d+\s*(odam|kishi|joy)\s+kerak\b")
        self.request_phrase_pattern = re.compile(
            r"\b(?:taxi|taksi|moshin|mashina)\s+kerak\b"
            r"|\b(?:yuradigan|yuradiglar)\s+bormi(?:kan)?\b"
            r"|\bkim\s+bor\b"
            r"|\bolib\s+ketadig(?:an|lar)\s+bormi\b"
        )
        self.offer_context_pattern = re.compile(
            r"\b(?:ketyapman|ketyapmiz|yuryapman|yuryapmiz|olib\s+ketaman|olibketaman|"
            r"olib\s+ketamiz|olibketamiz|zakazga(?:\s+ham)?\s+yuraman|manzildan\s+manzilgach|"
            r"joy\s+bor|bagaj|pochta|shafer|shafermiz|haydovchimiz|yuraman|ketaman|boraman|chiqaman|chiqamiz|komfort)\b"
        )
        self.vehicle_model_pattern = re.compile(
            r"\b(?:kobalt|cobalt|nexia|jentra|malibu|lacetti|damas|spark|captiva|onix|tracker|matiz|epica)\b"
        )
        self.phone_pattern = re.compile(r"\b(?:998)?\d{7,12}\b")

        self._sync_dynamic_keywords(force=True)

    def evaluate(self, normalized_text: str) -> FastFilterResult:
        self._sync_dynamic_keywords()
        min_length = (
            self.runtime_config.snapshot().min_text_length if self.runtime_config else self.min_length
        )
        if not normalized_text:
            return FastFilterResult(False, "empty_text", 0)

        tokens = tokenize(normalized_text)
        if (
            len(normalized_text) < min_length
            and not self.route_pattern.search(normalized_text)
            and not self.suffix_route_pattern.search(normalized_text)
        ):
            return FastFilterResult(False, "too_short", 0)

        if any(token in self.exclude_tokens for token in tokens):
            return FastFilterResult(False, "exclude_keyword", 0)

        stemmed_tokens = [self._stem_token(token) for token in tokens]
        transport_hits = self._count_fuzzy_hits(tokens, self.transport_tokens, self._transport_by_len)
        request_hits = self._count_fuzzy_hits(tokens, self.request_tokens, self._request_by_len)
        offer_hits = self._count_fuzzy_hits(tokens, self.offer_tokens, self._offer_by_len)
        location_hits = self._count_fuzzy_hits(stemmed_tokens, self.location_tokens, self._location_by_len)
        route_hits = self._count_exact_hits(tokens, self.route_tokens)
        has_route = bool(self.route_pattern.search(normalized_text)) or bool(self.suffix_route_pattern.search(normalized_text))
        has_people = bool(self.people_pattern.search(normalized_text))
        has_passenger_needed = bool(self.passenger_needed_pattern.search(normalized_text))
        has_request_phrase = bool(self.request_phrase_pattern.search(normalized_text))
        has_offer_context = bool(self.offer_context_pattern.search(normalized_text))
        has_vehicle_model = bool(self.vehicle_model_pattern.search(normalized_text))
        has_phone = bool(self.phone_pattern.search(normalized_text))
        has_region = self.geo.detect_region(normalized_text) is not None

        offer_dominant = (
            has_offer_context
            or has_vehicle_model
            or (offer_hits > 0 and has_passenger_needed)
            or (has_passenger_needed and has_route and transport_hits > 0)
        )
        if offer_hits > 0 and not has_request_phrase:
            return FastFilterResult(False, "likely_taxi_offer", 0)
        if offer_dominant and not has_request_phrase:
            return FastFilterResult(False, "likely_taxi_offer", 0)

        score = 0
        if has_request_phrase:
            score += 2
        elif request_hits:
            score += 1
        if transport_hits:
            score += 1
        if has_route or route_hits >= 1:
            score += 2
        if location_hits or has_region:
            score += 1
        if has_people:
            score += 1
        if has_phone:
            score += 1

        request_signal = has_request_phrase or request_hits > 0
        if score >= 4 and request_signal and not offer_dominant:
            return FastFilterResult(True, "candidate_order", score)
        if (
            score >= 3
            and request_signal
            and (location_hits > 0 or has_region or has_route)
            and not offer_dominant
        ):
            return FastFilterResult(True, "candidate_order", score)
        return FastFilterResult(False, "no_order_signal", score)

    def _sync_dynamic_keywords(self, force: bool = False) -> None:
        if not self.keyword_service:
            if self._keyword_version >= 0 and not force:
                return
            self._apply_keywords(
                transport={"taxi", "taksi", "yandex", "moshin", "mashina", "avto"},
                request={"kerak", "bormi", "buyurtma", "zakaz", "yuradigan", "yuradiglar"},
                offer={
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
                },
                location={"toshkent", "andijon", "namangan", "fargona", "samarqand", "nukus", "buxoro"},
                route={"dan", "ga", "from", "to"},
                exclude={"vakansiya", "reklama", "kurs", "kanal"},
            )
            self._keyword_version = 0
            return

        snapshot = self.keyword_service.snapshot()
        if not force and snapshot.version == self._keyword_version:
            return
        self._apply_keywords(
            transport=set(snapshot.transport),
            request=set(snapshot.request),
            offer=set(snapshot.offer),
            location=set(snapshot.location),
            route=set(snapshot.route),
            exclude=set(snapshot.exclude),
        )
        self._keyword_version = snapshot.version

    def _apply_keywords(
        self,
        transport: set[str],
        request: set[str],
        offer: set[str],
        location: set[str],
        route: set[str],
        exclude: set[str],
    ) -> None:
        self.transport_tokens = transport
        self.request_tokens = request
        self.offer_tokens = offer
        self.location_tokens = location
        self.route_tokens = route
        self.exclude_tokens = exclude
        self._transport_by_len = self._build_vocab_by_len(transport)
        self._request_by_len = self._build_vocab_by_len(request)
        self._offer_by_len = self._build_vocab_by_len(offer)
        self._location_by_len = self._build_vocab_by_len(location)

    @staticmethod
    def _count_exact_hits(tokens: list[str], vocab: set[str]) -> int:
        return len({token for token in tokens if token in vocab})

    @staticmethod
    def _build_vocab_by_len(vocab: set[str]) -> dict[int, tuple[str, ...]]:
        by_len: dict[int, list[str]] = {}
        for value in vocab:
            by_len.setdefault(len(value), []).append(value)
        return {length: tuple(values) for length, values in by_len.items()}

    @staticmethod
    def _stem_token(token: str) -> str:
        for suffix in ("lardan", "dan", "ga", "ni", "da"):
            if token.endswith(suffix) and len(token) > len(suffix) + 2:
                return token[: -len(suffix)]
        return token

    def _count_fuzzy_hits(
        self, tokens: list[str], vocab: set[str], vocab_by_len: dict[int, tuple[str, ...]]
    ) -> int:
        matched: set[str] = set()
        for token in tokens:
            if token in vocab:
                matched.add(token)
                continue
            if len(token) < 4:
                continue

            candidates = (
                vocab_by_len.get(len(token) - 1, ())
                + vocab_by_len.get(len(token), ())
                + vocab_by_len.get(len(token) + 1, ())
            )
            for candidate in candidates:
                if self._is_one_edit_or_less(token, candidate):
                    matched.add(candidate)
                    break
        return len(matched)

    @staticmethod
    def _is_one_edit_or_less(a: str, b: str) -> bool:
        if a == b:
            return True
        len_a = len(a)
        len_b = len(b)
        if abs(len_a - len_b) > 1:
            return False

        if len_a == len_b:
            diff = 0
            for ca, cb in zip(a, b, strict=False):
                if ca != cb:
                    diff += 1
                    if diff > 1:
                        return False
            return True

        if len_a > len_b:
            a, b = b, a
            len_a, len_b = len_b, len_a

        i = 0
        j = 0
        edits = 0
        while i < len_a and j < len_b:
            if a[i] == b[j]:
                i += 1
                j += 1
                continue
            edits += 1
            if edits > 1:
                return False
            j += 1

        if j < len_b:
            edits += 1
        return edits <= 1
