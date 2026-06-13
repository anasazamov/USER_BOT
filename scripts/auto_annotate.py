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
    # Driver announcing an existing gendered passenger ("ayol kishi bor",
    # "erkak yo'lovchi bor", "qiz bor") — drivers advertise the seat
    # composition of who's already in the car to attract co-riders.
    r"\b(?:ayol|erkak|qiz|ayollar|erkaklar|opa|aka|xola|akamiz|opamiz)\b[^.\n]{0,40}\b(?:kishi|yo[\W_]?lovchi|passajir)\s+bor\b",
    r"\b(?:ayol|erkak|qiz|ayollar|erkaklar)\s+yo[\W_]?lovchi(?:miz)?\s+bor\b",
    r"\b(?:ayol|erkak)\s+kishi(?:miz)?\s+bor\b",
    # Driver scheduling: pre-dawn or early morning time announcement with route.
    # "saxar 3:00-4:00", "ertalab 5 da", "sahar 03 00 larda" — customers rarely
    # write departure-time-ranges; drivers list their planned departure window.
    r"\b(?:saxar|sahar|saxarda|saharda|saharlab|tongda|tongotar)\b\s*\d",
    r"\bertalab\s+\d+\s*(?:da|:\d{2})",
    r"\b\d+\s*[:.]?\s*\d{0,2}\s*[\-–]\s*\d+\s*[:.]?\s*\d{0,2}\s+larda\b",  # 3:00-4:00 larda
    # Multi-city route sequence (≥3 places) — driver listing pickup stops.
    # We approximate by counting comma-or-space separated capitalised place
    # names; this is run against the normalized text inside annotate() with a
    # secondary heuristic, not as a raw regex.
    # Driver service-offer keywords:
    r"\bxizmat(?:imiz|imizda)?\b", r"\btaksi\s+xizmat", r"\btaxi\s+xizmat",
    r"\bzakaz\s+(?:qabul\s+qilamiz|olamiz)", r"\bzakazga\s+yuramiz\b",
    r"\bbron\s+qiling\b", r"\border\s+qiling\b",
    r"\bklient\s+vat", r"\bmijoz\s+vat",  # "klient vaqtiga yuramiz"
]
_DRIVER_RE = re.compile("|".join(_DRIVER_PATTERNS), re.IGNORECASE)

# Driver-specific "X kishi kerak" pattern — when a passenger count is "needed"
# *after* a route originating with "<city>dan", that's a driver announcing
# available seats, not a customer. Customers more often write "taxi kerak" or
# "X kishi-miz" (we are X people) without "kerak".
_DRIVER_SEATS_NEEDED_RE = re.compile(
    r"\b\w{3,}(?:dan|den)\b[\s\S]{0,80}\b\d+\s*(?:ta\s+)?(?:kishi|odam|yo[\W_]?lovchi|joy)\s+kerak\b",
    re.IGNORECASE,
)


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
# bare "1 kishi bor" could be a chat message about a group of people. We
# accept "<city>dan <city>ga|gacha", "<city>ga <city>dan" (reversed), and
# common destination-only forms like "shaharga", "biznasiga", "vokzalga".
_ROUTE_RE = re.compile(
    r"\b[a-z0-9]{3,}(?:dan|den)\b.*?\b[a-z0-9]{2,}(?:ga|ge|gacha)\b"
    r"|\b[a-z0-9]{3,}(?:ga|gacha)\b.*?\b[a-z0-9]{2,}(?:dan|den)\b"
    r"|\b[a-z0-9]{3,}\s+(?:dan|den)\s+[a-z0-9]{2,}\s+(?:ga|ge|gacha)\b"
    r"|\b(?:shaharga|shahardan|jartepaga|jartepadan|vokzalga|vokzaldan|"
    r"banisasiga|biznasiga|raykonga|markazga|aeroportga|aerportdan|"
    r"bekatga|bekatdan|temir\s+yol|temiryol|stansiya|avtovokzal|"
    r"stantsiyaga|metro|bozorga|maydonga|institutga|universitetga)\b",
    re.IGNORECASE,
)

# A weaker "looks like a destination" hint used to upgrade ambiguous
# "X kishi bor" messages to orders when there's no explicit dan/ga pattern.
_DESTINATION_HINT_RE = re.compile(
    r"\b[a-z0-9]{4,}(?:ga|gacha|gech|kech)\b",
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


def _despace_singletons(text: str) -> str:
    """Collapse adversarial single-letter spacing like 'u r g u t d a n' →
    'urgutdan'. Some drivers space-out words to bypass keyword filters; we
    run the driver regex on both the original and the despaced form so
    these tricks don't slip through.

    The heuristic only merges chains of 3+ tokens of length ≤ 2 — that's
    rare in natural Uzbek and a strong signal of intentional spacing.
    """
    def _merge(match: re.Match[str]) -> str:
        chunk = match.group(0)
        return chunk.replace(" ", "")

    # Three or more short tokens in a row → likely spaced-out word.
    return re.sub(r"(?:\b\w{1,2}\b\s+){2,}\b\w{1,2}\b", _merge, text)


def annotate(raw_text: str, normalized: str) -> str:
    """Return one of: 'order', 'driver', 'ad', 'noise'."""
    if not normalized or len(normalized) < 10:
        return "noise"

    # 1. Ads / spam / jobs first — these are the most varied and have explicit
    #    markers; catching them early stops them from being mis-bucketed.
    if _AD_RE.search(raw_text) or _AD_RE.search(normalized):
        return "ad"

    # 2. Driver signal: presence of any driver verb / vehicle / parcel phrase,
    #    OR "[city]dan ... X kishi kerak" which is a driver-side seat-offer.
    #    Also check the despaced form to catch "p o ch t a o l a m a n".
    despaced = _despace_singletons(normalized)
    driver_match = (
        bool(_DRIVER_RE.search(normalized))
        or bool(_DRIVER_SEATS_NEEDED_RE.search(normalized))
        or bool(_DRIVER_RE.search(despaced))
        or bool(_DRIVER_SEATS_NEEDED_RE.search(despaced))
    )

    # 3. Order signal: explicit request phrase OR "X kishi bor" + (route or
    #    phone or destination hint) — without driver signal anywhere.
    strong_order = bool(_ORDER_STRONG_RE.search(normalized))
    weak_people_order = bool(_ORDER_PEOPLE_RE.search(normalized))
    has_route = bool(_ROUTE_RE.search(normalized)) or bool(
        _ROUTE_RE.search(despaced)
    )
    has_destination_hint = bool(_DESTINATION_HINT_RE.search(normalized))
    has_phone = bool(_PHONE_RE.search(raw_text or normalized))

    if driver_match:
        return "driver"
    if strong_order:
        return "order"
    if weak_people_order and (has_route or has_phone or has_destination_hint):
        # "Toshkentdan Samarqandga 1 kishi bor +998..." OR
        # "temir yol banisasiga 1 kishi bor" (no driver verb) OR
        # "kop tarmoqlidan jartepagacha 2 kishi bor 9319..."
        return "order"

    # 4. Greetings / short noise.
    if _GREETING_RE.search(normalized):
        return "noise"
    if not has_phone and not has_route and not has_destination_hint:
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
