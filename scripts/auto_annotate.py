"""Auto-annotate raw Telegram messages for taxi-order classifier training.

Reads raw JSONL inputs, applies a layered rule set to each message, and writes
a single labeled JSONL with normalized text and one of four labels:

  - order      → customer asking for a ride (the positive class)
  - driver     → driver offering a ride (the dominant non-order in taxi groups)
  - ad         → channel/job/spam advertisement
  - noise      → greetings, off-topic chat, system messages

The labeling rules are intentionally regex-heavy and conservative on the
"order" side: a message is only labeled as ORDER when it contains an explicit
customer-side phrase (request verbs, "kim bor", "kishi bor" without driver
verbs nearby, "taxi kerak", etc.) AND lacks driver-side signals. Anything
ambiguous is bucketed into the non-order classes so the trained model errs
on the side of dropping rather than forwarding.

Output is consumed by scripts/train_classifier.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

from app.text import normalize_text


# Strip the bot's own forward wrapper if present, so we annotate the inner body.
_FORWARD_HEADER_RE = re.compile(r"^(?:taxi\s+buyurtma|buyurtma)\s*:\s*\n+", re.IGNORECASE)
_FORWARD_TAIL_RE = re.compile(
    r"\n+(?:#\S+|status\s*:\s*[^\n]*|manba\s*:\s*[^\n]*|aloqa\s*:\s*[^\n]*).*$",
    re.IGNORECASE | re.DOTALL,
)


# Strong driver-side indicators. Presence of any of these → label "driver".
# Includes plural and singular forms, common spelling variants, and
# driver-specific noun phrases ("pochta olamiz", "tom bagaj", "konditsioner").
_DRIVER_PATTERNS = [
    # Movement verbs in first-person plural/singular
    r"\bketamiz\b", r"\bketaman\b", r"\bketyapman\b", r"\bketyapmiz\b",
    r"\bboramiz\b", r"\bboraman\b", r"\bborayapman\b", r"\bborayapmiz\b",
    r"\byuramiz\b", r"\byuraman\b", r"\byuryapman\b", r"\byuryapmiz\b",
    r"\byuryamiz\b", r"\byuryaman\b",
    r"\bchiqamiz\b", r"\bchiqaman\b", r"\bchiqayapmiz\b",
    r"\bolaman\b", r"\bolamiz\b", r"\bolibketaman\b", r"\bolibketamiz\b",
    r"\bolib\s+ketaman\b", r"\bolib\s+ketamiz\b",
    r"\bolib\s+yuraman\b", r"\bolib\s+yuramiz\b",
    r"\bolib\s+boraman\b", r"\bolib\s+boramiz\b",
    # Parcel-taking phrases (driver service offering)
    r"\bpochta(?:yam|ham)?\s+olamiz\b", r"\bpochta(?:yam|ham)?\s+olaman\b",
    r"\bposhta(?:yam|ham)?\s+olamiz\b", r"\bposhta(?:yam|ham)?\s+olaman\b",
    r"\bposhtayam\s+olamiz\b", r"\bpochtayam\s+olamiz\b",
    r"\bpochta\s+bo[\W_]?lsa\s+olamiz\b",
    # Self-identification as driver
    r"\bhaydovchimiz\b", r"\bhaydovchi(?:miz|bor)\b", r"\bshafer\b", r"\bshafermiz\b",
    # Vehicle/equipment terms common in driver posts
    r"\bjoy\s+bor\b", r"\bmanzildan\s+manzilgach", r"\btom\s+bagaj\b",
    r"\bkomfort\b", r"\bkamfort\b", r"\bkondits", r"\bkonditsioner\b",
    r"\bavto\s+(?:propan|benzin)\b", r"\bpropan\b", r"\bbenzin\b",
    r"\bwifi\b", r"\bwi[-_\s]*fi\b", r"\binternet\s+bor\b",
    # Vehicle models — almost exclusively driver advertising
    r"\b(?:kobalt|cobalt|nexia|jentra|gentra|malibu|lacetti|damas|"
    r"captiva|onix|tracker|matiz|epica|labo|tico|spark|larcetti|"
    r"isuzu|mercedes|lacceti|sparkle|cubalt|kabalt|matis)\b",
    # "Srochna [route] yuramiz" / "ertaga ertalab [route] ketamiz" style scheduling
    r"\bsrochn[oa]\s+\w+\s+(?:yuramiz|ketamiz|olamiz)\b",
    # Quantity + driver verb ("2 kishi olamiz", "3 ta odam ketamiz")
    r"\b\d+\s*(?:ta\s+)?(?:odam|kishi|passajir|yo[\W_]?lovchi|joy)\s+(?:olamiz|olaman)\b",
]
_DRIVER_RE = re.compile("|".join(_DRIVER_PATTERNS), re.IGNORECASE)


# Customer-side order indicators. A real order must match at least one of
# these AND have NO driver indicator. The "X kishi bor" pattern is checked
# separately so we can require the absence of driver verbs nearby.
_ORDER_STRONG_PATTERNS = [
    r"\bkim\s+bor\b",
    r"\btaxi\s+kerak\b", r"\btaksi\s+kerak\b",
    r"\bmoshina\s+kerak\b", r"\bmashina\s+kerak\b",
    r"\bolib\s+ketadi(?:gan)?(?:\s+(?:kim|bor|bormi))?\b",
    r"\bketadigan\s+(?:kim|bormi)\b",
    r"\byuradigan\s+(?:kim|bormi)\b",
    r"\bjo[\W_]?nayotgan\s+(?:kim|bormi)\b",
    r"\bborayotgan\s+(?:kim|bormi)\b",
    r"\bola\s+ketadi(?:gan|miz)?\s+bormi\b",
    r"\botadigan\s+bor(?:mi)?\b",
]
_ORDER_STRONG_RE = re.compile("|".join(_ORDER_STRONG_PATTERNS), re.IGNORECASE)

# Weaker order pattern: "X kishi bor" / "1 odam bor" used by a passenger
# announcing they need a ride. Only counts as order if no driver signal is
# present in the message.
_ORDER_PEOPLE_RE = re.compile(
    r"\b(?:\d+\s*(?:ta\s+)?)?(?:odam|kishi|passajir|yo[\W_]?lovchi)\s+(?:bor|bot)\b",
    re.IGNORECASE,
)

# Route pattern — needed to confirm an "order" has direction; otherwise a
# bare "1 kishi bor" could be a chat message about a group of people.
_ROUTE_RE = re.compile(
    r"\b[a-z0-9]{3,}(?:dan|den)\b.*\b[a-z0-9]{2,}(?:ga|ge|gacha)\b"
    r"|\b[a-z0-9]{3,}\s+(?:dan|den)\s+[a-z0-9]{2,}\s+(?:ga|ge|gacha)\b"
    r"|\bshaharga\b"
    r"|\bshahardan\b",
    re.IGNORECASE,
)

# Phone number (broad pattern, used to discriminate orders vs greetings).
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?998)?[\s\-()]*(?:\d[\s\-()]*){7,12}(?!\d)")


# Advertisement / job / channel-promo / spam indicators.
_AD_PATTERNS = [
    r"https?://", r"\bt\.me/\S+",
    r"\bobuna\s+bo[\W_]?ling\b", r"\bkanalimiz", r"\ba[\W_]?zo\s+bo[\W_]?ling\b",
    r"\bguruh(?:imiz|ga\s+a[\W_]?zo|imizga)\b",
    r"\bvakansiya\b", r"\bish\s+(?:bor|topish|joyi|izlash)\b", r"\bishchi\b",
    r"\bonline\s+ish\b", r"\bishga\s+olamiz\b", r"\bishga\s+kerak\b",
    r"\bkredit\b", r"\binvestitsiya\b", r"\b(?:bitcoin|crypto|kripto|trading|forex)\b",
    r"\b(?:sotiladi|sotaman|sotamiz)\b", r"\bnarx(?:lari)?\b", r"\$\s*\d",
    r"\breklama\b", r"\baksiya\b", r"\bchegirma\b", r"\bpromo\b",
    r"\bmaxsulot(?:lar(?:imiz)?)?\b",
    r"\bguruhda\s+yozish\s+uchun\s+\d+\s+ta\s+odam\b",  # bot's "add N people to write" prompts
]
_AD_RE = re.compile("|".join(_AD_PATTERNS), re.IGNORECASE)


_GREETING_RE = re.compile(
    r"^\s*(?:assalomu\s+a?layku?m|salom|salomlar|xush\s+kelibsiz|yaxshimisiz|"
    r"rahmat|tahmin|ok|tushundim)\b",
    re.IGNORECASE,
)


def strip_forward_wrapper(text: str) -> str:
    if not text:
        return text
    body = _FORWARD_HEADER_RE.sub("", text, count=1)
    body = _FORWARD_TAIL_RE.sub("", body, count=1)
    return body.strip() or text.strip()


def annotate(raw_text: str, normalized: str) -> str:
    """Return one of: 'order', 'driver', 'ad', 'noise'."""
    if not normalized or len(normalized) < 10:
        return "noise"

    # 1. Ads / spam / jobs first — these are the most varied and have explicit
    #    markers; catching them early stops them from being mis-bucketed.
    if _AD_RE.search(raw_text) or _AD_RE.search(normalized):
        return "ad"

    # 2. Driver signal: presence of any driver verb / vehicle / parcel phrase.
    driver_match = bool(_DRIVER_RE.search(normalized))

    # 3. Order signal: explicit request phrase OR "X kishi bor" + route + phone,
    #    without driver signal anywhere in the message.
    strong_order = bool(_ORDER_STRONG_RE.search(normalized))
    weak_people_order = bool(_ORDER_PEOPLE_RE.search(normalized))
    has_route = bool(_ROUTE_RE.search(normalized))
    has_phone = bool(_PHONE_RE.search(raw_text or normalized))

    if driver_match:
        return "driver"
    if strong_order:
        return "order"
    if weak_people_order and has_route:
        # "Toshkentdan Samarqandga 1 kishi bor +998..." with no driver verb
        return "order"

    # 4. Greetings / short noise.
    if _GREETING_RE.search(normalized):
        return "noise"
    if not has_phone and not has_route:
        return "noise"

    # 5. Catch-all: message has route/phone but no order or driver verb —
    #    most often noise / off-topic / partial messages.
    return "noise"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto-annotate raw messages for taxi classifier.")
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--output", default="data/auto_annotated.jsonl")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    seen: set[tuple[int, int]] = set()
    counts: Counter[str] = Counter()
    written = 0

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as out:
        for input_path in args.inputs:
            try:
                with open(input_path, encoding="utf-8") as fp:
                    for line in fp:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        chat_id = row.get("chat_id", 0)
                        message_id = row.get("message_id", 0)
                        key = (chat_id, message_id)
                        if key in seen:
                            continue
                        seen.add(key)

                        raw = row.get("raw_text") or row.get("raw_preview") or ""
                        if not raw:
                            continue
                        body = strip_forward_wrapper(raw)
                        normalized = normalize_text(body)
                        label = annotate(body, normalized)

                        out.write(
                            json.dumps(
                                {
                                    "chat_id": chat_id,
                                    "message_id": message_id,
                                    "body": body,
                                    "normalized": normalized,
                                    "label": label,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        counts[label] += 1
                        written += 1
            except FileNotFoundError:
                print(f"[warn] missing input: {input_path}", file=sys.stderr)
                continue

    print(f"[info] wrote {written} labeled rows to {output_path}")
    total = sum(counts.values()) or 1
    for label, n in counts.most_common():
        print(f"  {label:8} {n:6} ({n/total*100:.1f}%)")


if __name__ == "__main__":
    main()
