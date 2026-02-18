from __future__ import annotations

import re
import unicodedata

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]+",
    flags=re.UNICODE,
)

_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "\u0430": "a",  # a
        "\u0431": "b",  # b
        "\u0432": "v",  # v
        "\u0433": "g",  # g
        "\u0434": "d",  # d
        "\u0435": "e",  # e
        "\u0451": "yo",  # yo
        "\u0436": "j",  # zh
        "\u0437": "z",  # z
        "\u0438": "i",  # i
        "\u0439": "y",  # y
        "\u043a": "k",  # k
        "\u043b": "l",  # l
        "\u043c": "m",  # m
        "\u043d": "n",  # n
        "\u043e": "o",  # o
        "\u043f": "p",  # p
        "\u0440": "r",  # r
        "\u0441": "s",  # s
        "\u0442": "t",  # t
        "\u0443": "u",  # u
        "\u0444": "f",  # f
        "\u0445": "x",  # x
        "\u0446": "s",  # ts
        "\u0447": "ch",  # ch
        "\u0448": "sh",  # sh
        "\u0449": "sh",  # sh
        "\u044a": "",
        "\u044b": "i",
        "\u044c": "",
        "\u044d": "e",
        "\u044e": "yu",
        "\u044f": "ya",
        "\u049b": "q",
        "\u0493": "g",
        "\u04b3": "h",
        "\u045e": "o",
    }
)

_CONFUSABLE_TO_ASCII: dict[str, str] = {
    "\u1d00": "a",  # ᴀ
    "\u0299": "b",  # ʙ
    "\u1d04": "c",  # ᴄ
    "\u1d05": "d",  # ᴅ
    "\u1d07": "e",  # ᴇ
    "\ua730": "f",  # ꜰ
    "\u0262": "g",  # ɢ
    "\u029c": "h",  # ʜ
    "\u026a": "i",  # ɪ
    "\u1d0a": "j",  # ᴊ
    "\u1d0b": "k",  # ᴋ
    "\u029f": "l",  # ʟ
    "\u1d0d": "m",  # ᴍ
    "\u0274": "n",  # ɴ
    "\u1d0f": "o",  # ᴏ
    "\u1d18": "p",  # ᴘ
    "\u01eb": "q",  # ǫ
    "\u0280": "r",  # ʀ
    "\ua731": "s",  # ꜱ
    "\u1d1b": "t",  # ᴛ
    "\u1d1c": "u",  # ᴜ
    "\u1d20": "v",  # ᴠ
    "\u1d21": "w",  # ᴡ
    "\u028f": "y",  # ʏ
    "\u1d22": "z",  # ᴢ
    "\u0251": "a",  # ɑ
    "\u0250": "a",  # ɐ
    "\u0252": "o",  # ɒ
    "\u0259": "e",  # ə
    "\u025b": "e",  # ɛ
    "\u025c": "e",  # ɜ
    "\u0261": "g",  # ɡ
    "\u0268": "i",  # ɨ
    "\u0142": "l",  # ł
    "\u019a": "l",  # ƚ
    "\u026f": "m",  # ɯ
    "\u0272": "n",  # ɲ
    "\u014b": "n",  # ŋ
    "\u0254": "o",  # ɔ
    "\u03b1": "a",  # α
    "\u03b2": "b",  # β
    "\u03b5": "e",  # ε
    "\u03b9": "i",  # ι
    "\u03ba": "k",  # κ
    "\u03bc": "m",  # μ
    "\u03bd": "v",  # ν
    "\u03bf": "o",  # ο
    "\u03c1": "r",  # ρ
    "\u03c4": "t",  # τ
    "\u03c5": "y",  # υ
    "\u03c7": "x",  # χ
    "\u0455": "s",  # ѕ
    "\u0456": "i",  # і
    "\u0458": "j",  # ј
    "\u04cf": "l",  # ӏ
    "\u0501": "d",  # ԁ
    "\u00e6": "ae",  # æ
    "\u0153": "oe",  # œ
}

_APOSTROPHE_RE = re.compile(r"[\u0060\u00b4\u0027\u2019\u02bb\u02bc\u02b9]")
_INVISIBLE_RE = re.compile(
    r"[\u00ad\u034f\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)
_NOISE_CHARS_RE = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_RE = re.compile(r"\s+")
_REPEATED_CHAR_RE = re.compile(r"([a-z])\1{2,}")


def _fold_confusables(text: str) -> str:
    return "".join(_CONFUSABLE_TO_ASCII.get(char, char) for char in text)


def _strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_text(text: str) -> str:
    # NFKC helps collapse full-width and stylized glyphs into canonical forms.
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _INVISIBLE_RE.sub("", normalized)
    normalized = _fold_confusables(normalized)
    normalized = _strip_diacritics(normalized)
    normalized = _APOSTROPHE_RE.sub("", normalized)
    normalized = normalized.translate(_CYRILLIC_TO_LATIN)
    normalized = _EMOJI_RE.sub(" ", normalized)
    normalized = normalized.replace("->", " ").replace("=>", " ")
    normalized = normalized.replace("-", " ").replace("_", " ")
    normalized = normalized.replace("/", " ").replace("|", " ")
    normalized = _NOISE_CHARS_RE.sub(" ", normalized)
    normalized = _REPEATED_CHAR_RE.sub(r"\1\1", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [token for token in text.split(" ") if token]
