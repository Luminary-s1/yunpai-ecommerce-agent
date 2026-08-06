"""Re-run the leaked 40-case acceptance corpus with R7 scoring semantics."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import shlex
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ecommerce_agent.config import Settings
from ecommerce_agent.intent import classify
from ecommerce_agent.llm import ModelGateway


ROOT = Path(__file__).resolve().parents[2]


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


def ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    load_env_file(args.env_file)
    settings = Settings.from_env()
    model = ModelGateway(settings)
    healthy, reason = model.health()
    if not healthy:
        model.close()
        raise SystemExit(f"model gateway is not configured: {reason}")

    sys.path.insert(0, str(ROOT / "tests"))
    namespace = runpy.run_path(str(ROOT / "tests" / "test_m4_acceptance.py"))
    corpus = namespace["INTENT_HOLDOUT"]
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))["records"]
    if len(corpus) != len(baseline):
        model.close()
        raise SystemExit("acceptance corpus and baseline record counts differ")

    records = []
    try:
        for index, (_, expected) in enumerate(corpus):
            message = corpus[index][0]
            started = time.perf_counter()
            result = classify(message, model=model)
            elapsed = time.perf_counter() - started
            records.append(
                {
                    "index": index,
                    "expected": expected,
                    "intent": result.intent,
                    "confidence": result.confidence,
                    "method": result.method,
                    "error": result.error,
                    "seconds": round(elapsed, 4),
                    "correct": result.method != "default" and result.intent == expected,
                }
            )
    finally:
        model.close()

    categories = {}
    for intent in ("product_inquiry", "after_sales", "complaint", "chitchat"):
        group = [record for record in records if record["expected"] == intent]
        answered = [record for record in group if record["method"] != "default"]
        categories[intent] = {
            "total": len(group),
            "answered": len(answered),
            "correct": sum(record["correct"] for record in group),
            "coverage": ratio(len(answered), len(group)),
            "answered_accuracy": ratio(
                sum(record["correct"] for record in answered), len(answered)
            ),
        }

    common_indexes = [
        index
        for index, record in enumerate(records)
        if record["method"] != "default" and baseline[index]["method"] != "default"
    ]
    common = {}
    for intent in categories:
        indexes = [
            index
            for index in common_indexes
            if records[index]["expected"] == intent
        ]
        common[intent] = {
            "count": len(indexes),
            "baseline_accuracy": ratio(
                sum(baseline[index]["intent"] == intent for index in indexes),
                len(indexes),
            ),
            "current_accuracy": ratio(
                sum(records[index]["intent"] == intent for index in indexes),
                len(indexes),
            ),
        }

    answered = [record for record in records if record["method"] != "default"]
    report = {
        "run_at": datetime.now(UTC).isoformat(),
        "dataset": "tests/test_m4_acceptance.py::INTENT_HOLDOUT",
        "dataset_status": "leaked regression only; not generalization evidence",
        "provider": settings.model_provider,
        "model": settings.model_name,
        "intent_budget_seconds": settings.intent_classify_timeout_seconds,
        "metrics": {
            "total": len(records),
            "answered": len(answered),
            "coverage": ratio(len(answered), len(records)),
            "correct": sum(record["correct"] for record in records),
            "answered_accuracy": ratio(
                sum(record["correct"] for record in answered), len(answered)
            ),
            "over_budget_count": sum(
                record["seconds"] > settings.intent_classify_timeout_seconds
                for record in records
            ),
            "categories": categories,
            "common_answered_subset": common,
        },
        "privacy": "messages omitted; records are joined to the leaked corpus by index",
        "records": records,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["metrics"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
