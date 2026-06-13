"""Train a taxi-order classifier from labeled.jsonl.

Binary task: order (positive) vs. not-order (haydovchi + reklama + none). The
not-order class lumps everything else because the production gap we want to
close is the false-positive rate — anything that isn't a real ride request
should be filtered out, regardless of *which* kind of non-order it is.

Pipeline: TF-IDF char_wb n-grams (3-5) + word n-grams (1-2), concatenated and
fed into a LogisticRegression with class_weight='balanced'. Char n-grams are
critical for Uzbek's heavy morphology and the latin/cyrillic mix in the data.

Output: data/classifier.joblib (vectorizer + model pickled together via
sklearn Pipeline). Loadable in app/classifier.py at runtime.
"""

from __future__ import annotations

import argparse
import json
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

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    classification_report,
    confusion_matrix,
    precision_recall_fscore_support,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import FeatureUnion, Pipeline

from app.text import normalize_text


POSITIVE_LABEL = "order"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train taxi-order vs not-order classifier.")
    parser.add_argument("--input", default="data/labeled.jsonl", help="Labeled JSONL input.")
    parser.add_argument(
        "--output", default="data/classifier.joblib", help="Trained model output path."
    )
    parser.add_argument(
        "--test-size",
        type=float,
        default=0.2,
        help="Fraction of data held out for evaluation. Default: 0.2",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed.")
    parser.add_argument(
        "--show-errors",
        type=int,
        default=10,
        help="Show this many misclassified test samples per class. Default: 10",
    )
    return parser.parse_args()


def load_labeled(path: Path) -> list[dict]:
    """Load labeled.jsonl, resolving undo markers."""
    seen: dict[tuple[int, int], dict] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = (row["chat_id"], row["message_id"])
            if row.get("undone"):
                seen.pop(key, None)
            else:
                seen[key] = row
    return list(seen.values())


def build_pipeline() -> Pipeline:
    char_vec = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(3, 5),
        min_df=2,
        sublinear_tf=True,
        lowercase=True,
    )
    word_vec = TfidfVectorizer(
        analyzer="word",
        ngram_range=(1, 2),
        min_df=2,
        sublinear_tf=True,
        lowercase=True,
        token_pattern=r"(?u)\b\w+\b",
    )
    features = FeatureUnion([("char", char_vec), ("word", word_vec)])
    clf = LogisticRegression(
        class_weight="balanced",
        max_iter=2000,
        C=1.0,
        solver="liblinear",
        random_state=42,
    )
    return Pipeline([("features", features), ("clf", clf)])


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = load_labeled(input_path)
    print(f"[info] loaded {len(rows)} labeled rows")

    label_counts = Counter(r["label"] for r in rows)
    for label, n in label_counts.most_common():
        print(f"  {label:12} {n}")

    texts = [normalize_text(r.get("body") or r.get("raw_text", "")) for r in rows]
    y = [1 if r["label"] == POSITIVE_LABEL else 0 for r in rows]

    # Filter out empty texts (post-normalize) — they carry no signal.
    pairs = [(t, label) for t, label in zip(texts, y) if t.strip()]
    if len(pairs) < len(texts):
        print(f"[info] dropped {len(texts) - len(pairs)} empty-after-normalize rows")
    texts, y = zip(*pairs)

    pos_count = sum(y)
    print(f"[info] positives (order): {pos_count}, negatives: {len(y) - pos_count}")

    X_train, X_test, y_train, y_test = train_test_split(
        texts, y, test_size=args.test_size, random_state=args.seed, stratify=y
    )

    pipe = build_pipeline()
    pipe.fit(X_train, y_train)

    y_pred = pipe.predict(X_test)
    y_proba = pipe.predict_proba(X_test)[:, 1]

    print("\n=== Test set evaluation ===")
    print(classification_report(y_test, y_pred, target_names=["not_order", "order"], digits=3))

    cm = confusion_matrix(y_test, y_pred)
    print("Confusion matrix (rows=true, cols=pred):")
    print(f"             pred_not_order  pred_order")
    print(f"not_order    {cm[0][0]:14d}  {cm[0][1]:10d}")
    print(f"order        {cm[1][0]:14d}  {cm[1][1]:10d}")

    p, r, f, _ = precision_recall_fscore_support(y_test, y_pred, average="binary")
    print(f"\nBinary (order=positive): precision={p:.3f} recall={r:.3f} f1={f:.3f}")

    # Show misclassifications so the user can see what's still confusing.
    if args.show_errors > 0:
        print("\n=== Misclassifications (test set) ===")
        false_pos = []  # predicted order, but isn't
        false_neg = []  # truly order, but missed
        for text, true_y, pred_y, proba in zip(X_test, y_test, y_pred, y_proba):
            if pred_y == 1 and true_y == 0:
                false_pos.append((proba, text))
            elif pred_y == 0 and true_y == 1:
                false_neg.append((proba, text))

        print(f"\n-- False positives (predicted order, was NOT) [{len(false_pos)} total]:")
        for proba, text in sorted(false_pos, key=lambda x: -x[0])[: args.show_errors]:
            print(f"  [conf={proba:.2f}] {text[:150]!r}")

        print(f"\n-- False negatives (was order, predicted NOT) [{len(false_neg)} total]:")
        for proba, text in sorted(false_neg, key=lambda x: x[0])[: args.show_errors]:
            print(f"  [conf={proba:.2f}] {text[:150]!r}")

    # Save full model. Retrain on ALL data for the final artifact — having squeezed
    # the test-set evaluation out, we now want every available label in the model.
    final_pipe = build_pipeline()
    final_pipe.fit(texts, y)
    joblib.dump(
        {
            "pipeline": final_pipe,
            "positive_label": POSITIVE_LABEL,
            "n_train_total": len(texts),
            "n_positive": pos_count,
        },
        output_path,
    )
    print(f"\n[info] model saved -> {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
