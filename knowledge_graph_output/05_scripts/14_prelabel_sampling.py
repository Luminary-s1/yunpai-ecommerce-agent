"""14_prelabel_sampling.py — AI 预标注 + 人工复核（提速人工标注）。

背景：60 条人工标注是验收证据，但纯人工从零判断耗时长（30-60 分钟）。
本脚本用大模型对每条样本预标注（TRUE/FALSE + 理由），人工只需**复核**
而非从零判断——把时间压到 5-10 分钟，同时保留"人工确认"的验收语义。

用法：
    python 05_scripts/14_prelabel_sampling.py
    （读环境变量 DEEPSEEK_API_KEY；或 --key 传）

产出：
    - 预标注写入 sampling_plan.csv 的 expected/annotation 列（人工仍可改）
    - 人工复核后运行 13_sampling_report.py 出正式准确率

注意：预标注是"建议"，不替代人工；验收报告仍报"60 条人工抽检（AI 预标+人复核）"。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import urllib.request
from pathlib import Path

REPORT_ROOT = Path(__file__).resolve().parent.parent / "06_report"
CLEAN_ROOT = Path(__file__).resolve().parent.parent / "02_clean"
PLAN_CSV = REPORT_ROOT / "sampling_plan.csv"

API_URL = "https://api.deepseek.com/chat/completions"


def build_entity_index() -> dict[str, str]:
    """从 02_clean 建实体 id → 名称 索引（预标注时给模型真实上下文，防幻觉）。"""
    index: dict[str, str] = {}
    # 每类文件的主键字段（sku.json 用 sku_id，product.json 用 item_id——不能混）
    key_fields = {
        "faq.json": ("faq_id", "question"),
        "policy.json": ("policy_code", "policy_name"),
        "script.json": ("script_id", "title"),
        "rule.json": ("rule_code", "rule_title"),
        "rule_extended.json": ("rule_code", "rule_title"),
        "product.json": ("item_id", "title"),
        "sku.json": ("sku_id", "title"),
        "category.json": ("category_code", "category_name"),
        "attribute.json": ("spec_key", "attr_key"),
    }
    for fname, (key_field, name_field) in key_fields.items():
        path = CLEAN_ROOT / fname
        if not path.exists():
            continue
        for rec in json.loads(path.read_text(encoding="utf-8")):
            eid = rec.get(key_field)
            if not eid:
                continue
            name = rec.get(name_field) or ""
            index[str(eid)] = f"{fname.replace('.json','')}:{str(name)[:60]}"
    return index


def load_plan() -> list[dict[str, str]]:
    with open(PLAN_CSV, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def call_model(api_key: str, prompt: str, model: str = "deepseek-chat") -> str:
    """调 deepseek chat completions，返回回复文本。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是电商知识库关系核验助手，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 200,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def prelabel(api_key: str, dry_run: bool = False) -> dict:
    """逐条预标注：给模型"关系 + 两端实体真实名称"，让它判 TRUE/FALSE。"""
    rows = load_plan()
    index = build_entity_index()
    results = {"labeled": 0, "true": 0, "false": 0, "error": 0}
    for i, r in enumerate(rows):
        rel = r["rel_type"]
        src = r["source"]
        tgt = r["target"]
        ev = r.get("evidence", "").strip()
        src_info = index.get(src, "（不在 02_clean，可能已删）")
        tgt_info = index.get(tgt, "（不在 02_clean，可能已删）")
        prompt = (
            f"判断电商知识库中这条关系是否成立（两端实体的真实信息已给出，直接判断，不要怀疑它们不存在）：\n"
            f"关系类型: {rel}\n"
            f"源实体: {src} = {src_info}\n"
            f"目标实体: {tgt} = {tgt_info}\n"
            f"证据: {ev or '（无）'}\n"
            f"判断标准：实体语义上属于该类别/与目标实体语义匹配→TRUE；明显不匹配→FALSE。\n"
            f"只输出 JSON: {{\"expected\": \"TRUE\" 或 \"FALSE\", \"reason\": \"一句话理由\"}}"
        )
        try:
            text = call_model(api_key, prompt)
            # 提取 JSON（模型可能带 markdown 代码块）
            start = text.find("{")
            end = text.rfind("}") + 1
            parsed = json.loads(text[start:end])
            expected = str(parsed.get("expected", "")).upper()
            reason = str(parsed.get("reason", ""))[:100]
            if expected not in ("TRUE", "FALSE"):
                raise ValueError(f"模型返回异常: {text[:80]}")
            r["expected"] = expected
            r["annotation"] = f"[AI预标] {reason}"
            r["verifier"] = "AI预标-待复核"
            if expected == "TRUE":
                results["true"] += 1
            else:
                results["false"] += 1
            results["labeled"] += 1
            print(f"[{i+1}/60] {rel} {src}->{tgt}: {expected} ({reason[:40]})")
        except Exception as exc:
            results["error"] += 1
            print(f"[{i+1}/60] {rel} {src}->{tgt}: ❌ {exc}")
    if not dry_run:
        with open(PLAN_CSV, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 预标注抽样（人工复核提速）")
    parser.add_argument("--key", default="", help="deepseek API key（默认读 DEEPSEEK_API_KEY）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写文件")
    args = parser.parse_args()
    api_key = args.key or os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("需要 --key 或 DEEPSEEK_API_KEY 环境变量")
        return 1
    results = prelabel(api_key, dry_run=args.dry_run)
    print(f"\n预标注完成: 共{len(load_plan())}条, 标注{results['labeled']}, "
          f"TRUE {results['true']} / FALSE {results['false']} / 错误 {results['error']}")
    print("→ 人工复核 sampling_plan.csv 后运行 13_sampling_report.py 出正式准确率")
    return 0


if __name__ == "__main__":
    main()
