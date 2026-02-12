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

_APOSTROPHE_RE = re.compile(r"[\u0060\u00b4\u0027\u2019\u02bb\u02bc\u02b9]")
_NOISE_CHARS_RE = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_RE = re.compile(r"\s+")
_REPEATED_CHAR_RE = re.compile(r"([a-z])\1{2,}")


def normalize_text(text: str) -> str:
    # NFKC helps collapse full-width and stylized glyphs into canonical forms.
    normalized = unicodedata.normalize("NFKC", text).lower()
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
