from __future__ import annotations

from dataclasses import dataclass

from app.text import tokenize


@dataclass(slots=True)
class RegionMatch:
    region_name: str
    hashtag: str
    confidence: int


class GeoResolver:
    def __init__(self) -> None:
        self.region_hashtags = {
            "Toshkent shahri": "#ToshkentShahri",
            "Toshkent viloyati": "#ToshkentViloyati",
            "Andijon viloyati": "#AndijonViloyati",
            "Namangan viloyati": "#NamanganViloyati",
            "Fargona viloyati": "#FargonaViloyati",
            "Sirdaryo viloyati": "#SirdaryoViloyati",
            "Jizzax viloyati": "#JizzaxViloyati",
            "Samarqand viloyati": "#SamarqandViloyati",
            "Buxoro viloyati": "#BuxoroViloyati",
            "Navoiy viloyati": "#NavoiyViloyati",
            "Qashqadaryo viloyati": "#QashqadaryoViloyati",
            "Surxondaryo viloyati": "#SurxondaryoViloyati",
            "Xorazm viloyati": "#XorazmViloyati",
            "Qoraqalpogiston Respublikasi": "#Qoraqalpogiston",
        }

        self.region_aliases = {
            "Toshkent shahri": {
                "toshkent",
                "tashkent",
                "toshkint",
                "tashkint",
                "toshkent shahar",
                "toshkent shahri",
                "tashkent city",
                "chilonzor",
                "sergeli",
                "yunusobod",
                "olmazor",
                "bektemir",
                "yakkasaroy",
                "uchtepa",
                "poytaxt",
            },
            "Toshkent viloyati": {
                "toshkent viloyati",
                "tashkent region",
                "chirchiq",
                "angren",
                "olmaliq",
                "bekobod",
                "yangiyol",
                "gazalkent",
                "parkent",
                "zangiota",
                "qibray",
                "chinoz",
            },
            "Andijon viloyati": {
                "andijon",
                "andijan",
                "asaka",
                "xonobod",
                "shahrixon",
                "marhamat",
                "baliqchi",
                "paxtaobod",
            },
            "Namangan viloyati": {
                "namangan",
                "chortoq",
                "chust",
                "pop",
                "uychi",
                "torakorgon",
                "turakurgan",
                "uchqorgon",
                "mingbuloq",
                "kosonsoy",
            },
            "Fargona viloyati": {
                "fargona",
                "fergana",
                "vodiy",
                "qoqon",
                "kokand",
                "margilon",
                "quva",
                "quvasoy",
                "rishton",
                "oltiariq",
                "beshariq",
                "bogdod",
            },
            "Sirdaryo viloyati": {
                "sirdaryo",
                "guliston",
                "yangiyer",
                "shirin",
                "boyovut",
                "xovos",
                "mirzaobod",
                "sayxunobod",
            },
            "Jizzax viloyati": {
                "jizzax",
                "zarbdor",
                "gallaorol",
                "forish",
                "paxtakor",
                "zomin",
                "dustlik",
                "baxmal",
            },
            "Samarqand viloyati": {
                "samarqand",
                "samarkand",
                "samarqan",
                "samarqannd",
                "urgut",
                "jartepa",
                "marhabo",
                "texnagazoil",
                "kattakorgon",
                "bulungur",
                "ishtixon",
                "pastdargom",
                "payariq",
                "jomboy",
                "narpay",
            },
            "Buxoro viloyati": {
                "buxoro",
                "bukhara",
                "gijduvon",
                "romitan",
                "vobkent",
                "qorakol",
                "karakul",
                "olot",
                "peshku",
                "shofirkon",
            },
            "Navoiy viloyati": {
                "navoiy",
                "navoi",
                "zarafshon",
                "uchquduq",
                "konimex",
                "karmana",
                "nurota",
                "tomdi",
                "xatirchi",
            },
            "Qashqadaryo viloyati": {
                "qashqadaryo",
                "qashkadaryo",
                "qarshi",
                "shahrisabz",
                "kitob",
                "guzor",
                "dehqonobod",
                "kasbi",
                "muborak",
                "yakkabog",
                "chiroqchi",
                "koson",
            },
            "Surxondaryo viloyati": {
                "surxondaryo",
                "surkhandarya",
                "termiz",
                "denov",
                "boysun",
                "sherobod",
                "jarqorgon",
                "qiziriq",
                "angor",
                "sariosiyo",
                "kumqorgon",
            },
            "Xorazm viloyati": {
                "xorazm",
                "khorezm",
                "urganch",
                "xiva",
                "khiva",
                "pitnak",
                "hazorasp",
                "shovot",
                "gurlan",
                "yangiarik",
                "bogot",
            },
            "Qoraqalpogiston Respublikasi": {
                "qoraqalpogiston",
                "qoraqalpaqstan",
                "karakalpakstan",
                "nukus",
                "beruniy",
                "kungirot",
                "kungrad",
                "taxiatosh",
                "chimboy",
                "moynaq",
                "turtkul",
                "ellikqala",
                "kegeyli",
                "shumanay",
            },
        }

        self._single_alias_to_region: dict[str, str] = {}
        self._phrase_aliases: list[tuple[str, str]] = []
        for region_name, aliases in self.region_aliases.items():
            for alias in aliases:
                if " " in alias:
                    self._phrase_aliases.append((alias, region_name))
                else:
                    self._single_alias_to_region[alias] = region_name

        self._single_alias_by_len = self._build_alias_by_len(self._single_alias_to_region.keys())

    def detect_region(self, normalized_text: str) -> RegionMatch | None:
        if not normalized_text:
            return None

        tokens = [self._stem_token(token) for token in tokenize(normalized_text)]
        scores: dict[str, int] = {}

        for phrase, region_name in self._phrase_aliases:
            if phrase in normalized_text:
                scores[region_name] = scores.get(region_name, 0) + 3

        for token in tokens:
            region_name = self._single_alias_to_region.get(token)
            if region_name:
                scores[region_name] = scores.get(region_name, 0) + 2
                continue
            fuzzy_region = self._fuzzy_region(token)
            if fuzzy_region:
                scores[fuzzy_region] = scores.get(fuzzy_region, 0) + 1

        if not scores:
            return None

        region_name, score = max(scores.items(), key=lambda item: item[1])
        if score < 2:
            return None
        return RegionMatch(
            region_name=region_name,
            hashtag=self.region_hashtags.get(region_name, "#Uzbekiston"),
            confidence=score,
        )

    @staticmethod
    def _build_alias_by_len(aliases: set[str] | list[str] | tuple[str, ...]) -> dict[int, tuple[str, ...]]:
        grouped: dict[int, list[str]] = {}
        for alias in aliases:
            grouped.setdefault(len(alias), []).append(alias)
        return {size: tuple(values) for size, values in grouped.items()}

    def _fuzzy_region(self, token: str) -> str | None:
        if len(token) < 4:
            return None
        candidates = (
            self._single_alias_by_len.get(len(token) - 1, ())
            + self._single_alias_by_len.get(len(token), ())
            + self._single_alias_by_len.get(len(token) + 1, ())
        )
        for candidate in candidates:
            if self._is_one_edit_or_less(token, candidate):
                return self._single_alias_to_region[candidate]
        return None

    @staticmethod
    def _stem_token(token: str) -> str:
        for suffix in ("lardan", "dan", "ga", "ni", "da", "lik"):
            if token.endswith(suffix) and len(token) > len(suffix) + 2:
                return token[: -len(suffix)]
        return token

    @staticmethod
    def _is_one_edit_or_less(a: str, b: str) -> bool:
        if a == b:
            return True
        if abs(len(a) - len(b)) > 1:
            return False

        if len(a) == len(b):
            diff = 0
            for ca, cb in zip(a, b, strict=False):
                if ca != cb:
                    diff += 1
                    if diff > 1:
                        return False
            return True

        if len(a) > len(b):
            a, b = b, a

        i = 0
        j = 0
        edits = 0
        while i < len(a) and j < len(b):
            if a[i] == b[j]:
                i += 1
                j += 1
                continue
            edits += 1
            if edits > 1:
                return False
            j += 1
        if j < len(b):
            edits += 1
        return edits <= 1
