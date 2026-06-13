"""Interactive CLI labeling tool for training data.

Reads raw JSONL inputs (forward target groups + source groups), strips the bot's
forward wrapper when present, and lets the operator label each message:

    o = order   (buyurtma — to'g'ri taksi buyurtmasi)
    h = haydovchi (taklif — driver offering ride)
    r = reklama (ads/spam/job/info)
    n = none     (boshqa, chat-chat, salom-alik)
    s = skip     (kelajakda qayta ko'rishga, hozir noaniq)
    u = undo     (oldingi yorliqni qaytarish)
    q = quit     (saqlab chiqish)

Labels are appended to data/labeled.jsonl. Resumable — already-labeled
message_ids are skipped.

Usage:
    python scripts/label_training_data.py \\
        --inputs data/training_raw.jsonl data/training_sources.jsonl \\
        --output data/labeled.jsonl \\
        --shuffle
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
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


# Matches the wrapper `app/actions.py` adds to forwarded messages:
#   "Taxi buyurtma:\n\n<BODY>\n\n#Region\n\nStatus: ...\n\nManba: ...\n\nAloqa: ..."
# Some forwards omit the leading header; some omit the hashtag/status. We strip
# greedily from both ends until only the order body remains.
_FORWARD_HEADER_RE = re.compile(r"^(?:taxi\s+buyurtma|buyurtma)\s*:\s*\n+", re.IGNORECASE)
_FORWARD_TAIL_RE = re.compile(
    r"\n+(?:#\S+|status\s*:\s*[^\n]*|manba\s*:\s*[^\n]*|aloqa\s*:\s*[^\n]*).*$",
    re.IGNORECASE | re.DOTALL,
)

LABELS = {
    "o": "order",
    "h": "haydovchi",
    "r": "reklama",
    "n": "none",
}


def strip_forward_wrapper(text: str) -> str:
    """If the text is a bot-formatted forward, return just the order body."""
    if not text:
        return text
    body = _FORWARD_HEADER_RE.sub("", text, count=1)
    body = _FORWARD_TAIL_RE.sub("", body, count=1)
    return body.strip() or text.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive labeler for taxi message classifier.")
    parser.add_argument("--inputs", nargs="+", required=True, help="Input JSONL files.")
    parser.add_argument("--output", default="data/labeled.jsonl", help="Labeled output JSONL.")
    parser.add_argument("--shuffle", action="store_true", help="Shuffle inputs before labeling.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for shuffle.")
    parser.add_argument(
        "--target",
        type=int,
        default=300,
        help="Stop after labeling this many new messages. Default: 300",
    )
    return parser.parse_args()


def load_inputs(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        if not p.exists():
            print(f"[warn] input missing: {p}", flush=True)
            continue
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def load_existing_labels(out_path: Path) -> tuple[dict[tuple[int, int], str], int]:
    existing: dict[tuple[int, int], str] = {}
    if not out_path.exists():
        return existing, 0
    with out_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = (row["chat_id"], row["message_id"])
            existing[key] = row["label"]
    return existing, len(existing)


def print_row(idx: int, total: int, row: dict, body: str) -> None:
    chat_title = row.get("chat_title", "")
    sender = row.get("sender_username") or row.get("sender_first_name") or "?"
    print()
    print("=" * 80)
    print(f"[{idx}/{total}]  chat={chat_title!r}  from=@{sender}")
    print("-" * 80)
    print(body[:1500])
    print("-" * 80)
    print("o=order  h=haydovchi  r=reklama  n=none  s=skip  u=undo  q=quit", flush=True)


def main() -> int:
    args = parse_args()

    inputs = [Path(p) for p in args.inputs]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    rows = load_inputs(inputs)
    if args.shuffle:
        random.Random(args.seed).shuffle(rows)
    existing, prior_count = load_existing_labels(output)
    print(f"[info] loaded {len(rows)} rows from {len(inputs)} files; already labeled: {prior_count}")

    pending = [r for r in rows if (r["chat_id"], r["message_id"]) not in existing]
    if not pending:
        print("[info] nothing left to label.")
        return 0
    print(f"[info] pending: {len(pending)}; session target: {args.target}")

    fp_out = output.open("a", encoding="utf-8")
    history: list[tuple[dict, dict]] = []  # (row, written_record) — for undo
    new_count = 0

    try:
        i = 0
        while i < len(pending) and new_count < args.target:
            row = pending[i]
            body = strip_forward_wrapper(row.get("raw_text", ""))
            if not body.strip():
                i += 1
                continue

            print_row(prior_count + new_count + 1, prior_count + new_count + len(pending) - i, row, body)
            choice = input("> ").strip().lower()
            if not choice:
                continue
            if choice == "q":
                break
            if choice == "s":
                i += 1
                continue
            if choice == "u":
                if not history:
                    print("[warn] nothing to undo")
                    continue
                last_row, last_record = history.pop()
                # We can't physically pop the last line of the JSONL without a rewrite,
                # so we record an "undo" marker; consumer can post-process.
                fp_out.write(json.dumps({**last_record, "undone": True}, ensure_ascii=False) + "\n")
                fp_out.flush()
                # Re-queue at current position so it gets re-shown.
                pending.insert(i, last_row)
                new_count -= 1
                continue
            if choice not in LABELS:
                print(f"[warn] unknown choice {choice!r}")
                continue

            label = LABELS[choice]
            record = {
                "chat_id": row["chat_id"],
                "message_id": row["message_id"],
                "chat_title": row.get("chat_title", ""),
                "raw_text": row.get("raw_text", ""),
                "body": body,
                "label": label,
            }
            fp_out.write(json.dumps(record, ensure_ascii=False) + "\n")
            fp_out.flush()
            history.append((row, record))
            new_count += 1
            i += 1
    finally:
        fp_out.close()

    print(f"\n[info] labeled {new_count} new (total now {prior_count + new_count}). saved -> {output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
