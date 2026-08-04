"""意图分类基准运行器。

不是测试，是测量。pytest 回答「有没有回归」，这里回答「现在有多好、差在哪」。
故意不放进 tests/：它会打真实模型、会花钱、结果是分数不是 pass/fail。

    python evals/intent/run.py --mode rule     # 只测规则层
    python evals/intent/run.py --mode mock     # 走 mock 网关
    python evals/intent/run.py --mode live     # 打真实模型（读环境变量）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from collections import Counter, defaultdict
from dataclasses import replace

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ecommerce_agent.config import Settings  # noqa: E402
from ecommerce_agent.intent import classify  # noqa: E402
from ecommerce_agent.llm import ModelGateway  # noqa: E402

CORPUS = pathlib.Path(__file__).with_name("corpus.jsonl")
INTENTS = ("product_inquiry", "after_sales", "complaint", "chitchat")


def load_corpus() -> list[dict]:
    rows = []
    for line_number, raw in enumerate(CORPUS.read_text("utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("//"):
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{CORPUS}:{line_number} 不是合法 JSON: {exc}") from exc
        if row["expected"] not in INTENTS:
            raise SystemExit(f"{CORPUS}:{line_number} expected 非法: {row['expected']}")
        rows.append(row)
    return rows


def build_model(mode: str):
    if mode == "rule":
        return None
    settings = Settings.from_env()
    if mode == "mock":
        settings = replace(settings, model_mock_mode=True, model_enabled=True)
    gateway = ModelGateway(settings)
    healthy, reason = gateway.health()
    if not healthy:
        gateway.close()
        raise SystemExit(
            f"模型网关不可用: {reason}。live 模式需要 MODEL_ENABLED=true 且配好 "
            f"MODEL_API_KEY；否则每一条都会静默降级成 default，跑出来的分数没有意义。"
        )
    print(f"# gateway: {reason}  timeout={settings.intent_classify_timeout_seconds}s")
    return gateway


def percent(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator * 100:5.1f}%" if denominator else "    --"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("rule", "mock", "live"), default="rule")
    parser.add_argument("--out", type=pathlib.Path, help="把逐条结果写成 JSON，便于两次运行 diff")
    parser.add_argument("--show", choices=("errors", "all", "none"), default="errors")
    parser.add_argument("--min-accuracy", type=float, default=None)
    parser.add_argument(
        "--request-interval",
        type=float,
        default=0.0,
        help="live 评测中两次真实模型尝试之间的最小间隔秒数",
    )
    args = parser.parse_args()
    request_interval = max(0.0, args.request_interval)

    corpus = load_corpus()
    model = build_model(args.mode)
    records = []
    try:
        for row in corpus:
            result = classify(row["message"], model=model)
            records.append(
                {
                    **row,
                    "predicted": result.intent,
                    "confidence": result.confidence,
                    "method": result.method,
                    "error": result.error,
                    "correct": result.intent == row["expected"],
                }
            )
            model_attempted = result.method == "model" or bool(
                result.error
                and result.error.startswith(
                    ("model_call_failed:", "model_payload_rejected:")
                )
            )
            if args.mode == "live" and model_attempted and request_interval:
                time.sleep(request_interval)
    finally:
        if model is not None:
            model.close()

    total = len(records)
    correct = sum(r["correct"] for r in records)
    # default 是「弃权」，不是「预测为 chitchat」。混在一起会让兜底把 chitchat 类的分数刷高。
    answered = [r for r in records if r["method"] != "default"]
    answered_correct = sum(r["correct"] for r in answered)

    print(f"\n=== 意图基准 · mode={args.mode} · n={total} ===")
    print(f"端到端准确率   {percent(correct, total)}  ({correct}/{total})")
    print(f"覆盖率(非弃权) {percent(len(answered), total)}  ({len(answered)}/{total})")
    print(f"判定准确率     {percent(answered_correct, len(answered))}  "
          f"({answered_correct}/{len(answered)})   ← 只看真正给了答案的")

    print("\n-- 按 method --")
    by_method: dict[str, list] = defaultdict(list)
    for record in records:
        by_method[record["method"]].append(record)
    for method in ("rule", "model", "default"):
        group = by_method.get(method, [])
        hit = sum(r["correct"] for r in group)
        print(f"{method:8} n={len(group):3}  准确 {percent(hit, len(group))}")

    print("\n-- 按真实类别 --")
    for intent in INTENTS:
        group = [r for r in records if r["expected"] == intent]
        hit = sum(r["correct"] for r in group)
        print(f"{intent:16} n={len(group):3}  召回 {percent(hit, len(group))}")

    print("\n-- 按标签（这里才看得出弱点分布）--")
    tag_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for record in records:
        for tag in record.get("tags", []):
            tag_stats[tag][0] += record["correct"]
            tag_stats[tag][1] += 1
    for tag, (hit, count) in sorted(tag_stats.items(), key=lambda kv: kv[1][0] / kv[1][1]):
        print(f"{tag:14} n={count:3}  准确 {percent(hit, count)}")

    confusions = Counter(
        (r["expected"], r["predicted"]) for r in records if not r["correct"]
    )
    if confusions:
        print("\n-- 主要混淆方向 --")
        for (expected, predicted), count in confusions.most_common(6):
            print(f"{expected:16} → {predicted:16} ×{count}")

    if args.show != "none":
        shown = records if args.show == "all" else [r for r in records if not r["correct"]]
        print(f"\n-- 逐条（{args.show}）--")
        for record in shown:
            mark = "✓" if record["correct"] else "✗"
            print(
                f"{mark} {record['id']:8} {record['method']:8} "
                f"{record['predicted']:16} (期望 {record['expected']:16}) "
                f"{record['message'][:28]!r}"
            )
            if not record["correct"] and record.get("note"):
                print(f"           ↳ {record['note']}")

    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "mode": args.mode,
                    "request_interval": request_interval,
                    "total": total,
                    "correct": correct,
                    "records": records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
        print(f"\n结果已写入 {args.out}")

    if args.min_accuracy is not None and correct / total < args.min_accuracy:
        print(f"\n低于阈值 {args.min_accuracy:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
