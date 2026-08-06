"""Run the frozen M4 complaint holdout with a configured live model."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ecommerce_agent.config import Settings  # noqa: E402
from ecommerce_agent.intent import classify  # noqa: E402
from ecommerce_agent.llm import ModelGateway  # noqa: E402


DEFAULT_CORPUS = Path(__file__).with_name("m4_complaint_holdout_v2.jsonl")
FORBIDDEN_EXPLICIT_TERMS = ("投诉", "差评", "举报", "曝光", "维权")


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


def load_corpus(path: Path) -> list[dict]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(rows) < 15:
        raise ValueError("complaint holdout requires at least 15 cases")
    if any(row.get("expected") != "complaint" for row in rows):
        raise ValueError("complaint holdout contains another expected intent")
    leaked = [
        row["id"]
        for row in rows
        if any(term in row["message"] for term in FORBIDDEN_EXPLICIT_TERMS)
    ]
    if leaked:
        raise ValueError(f"complaint holdout contains explicit keywords: {leaked}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    load_env_file(args.env_file)
    settings = Settings.from_env()
    model = ModelGateway(settings)
    healthy, reason = model.health()
    if not healthy:
        model.close()
        raise SystemExit(f"model gateway is not configured: {reason}")

    records = []
    try:
        for row in load_corpus(args.corpus):
            started = time.perf_counter()
            result = classify(row["message"], model=model)
            elapsed = time.perf_counter() - started
            records.append(
                {
                    **row,
                    "predicted": result.intent,
                    "confidence": result.confidence,
                    "method": result.method,
                    "error": result.error,
                    "elapsed_seconds": round(elapsed, 4),
                    "correct": result.method != "default"
                    and result.intent == row["expected"],
                }
            )
    finally:
        model.close()

    answered = [row for row in records if row["method"] != "default"]
    recall = sum(row["correct"] for row in records) / len(records)
    coverage = len(answered) / len(records)
    answered_accuracy = (
        sum(row["correct"] for row in answered) / len(answered) if answered else 0.0
    )
    over_budget = sum(
        row["elapsed_seconds"] > settings.intent_classify_timeout_seconds
        for row in records
    )
    report = {
        "dataset": args.corpus.name,
        "run_at": datetime.now(UTC).isoformat(),
        "provider": settings.model_provider,
        "model": settings.model_name,
        "intent_budget_seconds": settings.intent_classify_timeout_seconds,
        "source_note": (
            "20 条人工新造真实口语，未复制修复指南、验收测试或开发比较集；"
            "不含五个显式投诉关键词。文件创建后只执行一次最终 live 运行。"
        ),
        "metrics": {
            "complaint_recall": recall,
            "coverage": coverage,
            "answered_accuracy": answered_accuracy,
            "over_budget_count": over_budget,
            "total": len(records),
            "answered": len(answered),
        },
        "gate": {
            "passed": recall >= 0.75 and over_budget == 0,
            "min_complaint_recall": 0.75,
            "required_over_budget_count": 0,
        },
        "records": records,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    args.out.write_text(serialized + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"metrics": report["metrics"], "gate": report["gate"]},
            ensure_ascii=False,
        )
    )
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
