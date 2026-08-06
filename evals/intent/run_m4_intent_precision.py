"""Score complaint precision/recall on a JSONL corpus in rule or live mode."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path

from ecommerce_agent.config import Settings
from ecommerce_agent.intent import classify
from ecommerce_agent.llm import ModelGateway


INTENTS = frozenset(("product_inquiry", "after_sales", "complaint", "chitchat"))


def load_env_file(path: Path) -> None:
    in_code_block = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_block = not in_code_block
            continue
        if not in_code_block and not line.startswith("export "):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line or line.startswith("#"):
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        values = shlex.split(raw_value, comments=True, posix=True)
        os.environ[key] = values[0] if values else ""


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--mode", choices=("rule", "live"), required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dataset-status", default="unspecified")
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in args.corpus.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(rows) < 20 or any(row.get("expected") not in INTENTS for row in rows):
        raise SystemExit("corpus requires at least 20 rows with controlled intents")

    model = None
    settings = Settings.from_env()
    if args.mode == "live":
        if args.env_file is None:
            raise SystemExit("--env-file is required in live mode")
        load_env_file(args.env_file)
        settings = Settings.from_env()
        model = ModelGateway(settings)
        healthy, reason = model.health()
        if not healthy:
            model.close()
            raise SystemExit(f"model gateway is not configured: {reason}")

    records = []
    try:
        for row in rows:
            started = time.perf_counter()
            result = classify(row["message"], model=model)
            records.append(
                {
                    "id": row["id"],
                    "expected": row["expected"],
                    "predicted": result.intent,
                    "confidence": result.confidence,
                    "method": result.method,
                    "error": result.error,
                    "seconds": round(time.perf_counter() - started, 4),
                    "correct": result.method != "default"
                    and result.intent == row["expected"],
                }
            )
    finally:
        if model is not None:
            model.close()

    positives = [row for row in records if row["expected"] == "complaint"]
    negatives = [row for row in records if row["expected"] != "complaint"]
    predicted_complaints = [
        row
        for row in records
        if row["method"] != "default" and row["predicted"] == "complaint"
    ]
    true_positives = sum(row["expected"] == "complaint" for row in predicted_complaints)
    false_positives = len(predicted_complaints) - true_positives
    answered = [row for row in records if row["method"] != "default"]
    by_expected = {}
    for intent in sorted({row["expected"] for row in records}):
        group = [row for row in records if row["expected"] == intent]
        group_answered = [row for row in group if row["method"] != "default"]
        by_expected[intent] = {
            "total": len(group),
            "answered": len(group_answered),
            "coverage": ratio(len(group_answered), len(group)),
            "correct": sum(row["correct"] for row in group),
            "answered_accuracy": ratio(
                sum(row["correct"] for row in group_answered),
                len(group_answered),
            ),
        }
    precision = ratio(true_positives, len(predicted_complaints))
    recall = ratio(true_positives, len(positives))
    false_positive_rate = ratio(false_positives, len(negatives))
    over_budget_count = (
        sum(
            row["seconds"] > settings.intent_classify_timeout_seconds
            for row in records
        )
        if args.mode == "live"
        else 0
    )
    report = {
        "run_at": datetime.now(UTC).isoformat(),
        "dataset": args.corpus.name,
        "dataset_status": args.dataset_status,
        "mode": args.mode,
        "provider": settings.model_provider if args.mode == "live" else None,
        "model": settings.model_name if args.mode == "live" else None,
        "metrics": {
            "total": len(records),
            "positives": len(positives),
            "negatives": len(negatives),
            "answered": len(answered),
            "coverage": ratio(len(answered), len(records)),
            "complaint_true_positives": true_positives,
            "complaint_false_positives": false_positives,
            "complaint_precision": precision,
            "complaint_recall": recall,
            "negative_false_positive_rate": false_positive_rate,
            "answered_accuracy": ratio(
                sum(row["correct"] for row in answered), len(answered)
            ),
            "over_budget_count": over_budget_count,
            "by_expected": by_expected,
        },
        "gate": {
            "passed": bool(
                positives
                and negatives
                and precision is not None
                and precision >= 0.9
                and recall is not None
                and recall >= 0.75
                and false_positive_rate is not None
                and false_positive_rate <= 0.05
                and over_budget_count == 0
            ),
            "min_complaint_precision": 0.9,
            "min_complaint_recall": 0.75,
            "max_negative_false_positive_rate": 0.05,
            "required_over_budget_count": 0,
        },
        "records": records,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"metrics": report["metrics"], "gate": report["gate"]},
            ensure_ascii=False,
        )
    )
    return 0 if args.mode == "rule" or report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
