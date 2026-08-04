"""Measure rule-to-model routing without depending on model accuracy or availability."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ecommerce_agent.intent import classify  # noqa: E402

CORPUS = pathlib.Path(__file__).with_name("cross_domain_holdout.jsonl")
ROUTES = frozenset(("rule", "model"))


class RouteProbeModel:
    settings = SimpleNamespace(intent_classify_timeout_seconds=0.01)

    def __init__(self) -> None:
        self.calls = 0

    def generate_json(self, _messages, *, timeout_seconds):
        self.calls += 1
        return {"intent": "chitchat", "confidence": 0.5}


def load_corpus() -> list[dict]:
    rows = []
    for line_number, raw in enumerate(CORPUS.read_text("utf-8").splitlines(), 1):
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{CORPUS}:{line_number} 不是合法 JSON: {exc}") from exc
        if row.get("expected_route") not in ROUTES:
            raise SystemExit(
                f"{CORPUS}:{line_number} expected_route 非法: "
                f"{row.get('expected_route')}"
            )
        if row["expected_route"] == "rule" and not row.get("expected_intent"):
            raise SystemExit(f"{CORPUS}:{line_number} rule 样例缺 expected_intent")
        rows.append(row)
    return rows


def percent(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator * 100:5.1f}%" if denominator else "    --"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args()

    records = []
    for row in load_corpus():
        model = RouteProbeModel()
        result = classify(row["message"], model=model)
        route_ok = result.method == row["expected_route"]
        intent_ok = (
            row["expected_route"] != "rule"
            or result.intent == row["expected_intent"]
        )
        records.append(
            {
                **row,
                "actual_route": result.method,
                "actual_intent": result.intent,
                "model_calls": model.calls,
                "correct": route_ok and intent_ok,
            }
        )

    print(f"\n=== cross-domain 路由留出集 · n={len(records)} ===")
    for expected_route, label in (
        ("model", "跨域仲裁召回"),
        ("rule", "业务快路径保留"),
    ):
        group = [r for r in records if r["expected_route"] == expected_route]
        hit = sum(r["correct"] for r in group)
        print(f"{label:16} {percent(hit, len(group))}  ({hit}/{len(group)})")

    print("\n-- 逐条 --")
    for record in records:
        mark = "✓" if record["correct"] else "✗"
        print(
            f"{mark} {record['id']:7} {record['actual_route']:7} "
            f"{record['actual_intent']:16} {record['message']!r}"
        )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {"total": len(records), "records": records},
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
        print(f"\n结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
