"""13_sampling_report.py — 人工标注回填后的抽检准确率统计（验收证据链）。

读取 06_report/sampling_plan.csv（07_sampling.py 生成、人工填写 expected 列），
统计并产出 06_report/sampling_accuracy_report.md：

- 已标注条数 / 总条数
- 准确率 = 标注为 TRUE 且与关系本身一致？——按"人工判定关系是否成立"统计：
    expected 填 TRUE = 该关系成立；FALSE = 不成立（可能关系错误或方向反）
- 按关系类型分层的准确率
- 未标注明细（剩余待办）

用法：
    python 05_scripts/13_sampling_report.py

验收口径：
    - 60 条全部标注后，报告给出真实准确率（取代此前"自动 100%"的虚假数字）
    - verifier / verified_at 列体现人工背书
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REPORT_ROOT = Path(__file__).resolve().parent.parent / "06_report"
PLAN_CSV = REPORT_ROOT / "sampling_plan.csv"
OUT_MD = REPORT_ROOT / "sampling_accuracy_report.md"

VALID = {"TRUE", "FALSE", "N/A"}


def load_plan() -> list[dict[str, str]]:
    with open(PLAN_CSV, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    rows = load_plan()
    total = len(rows)
    annotated = [r for r in rows if (r.get("expected") or "").strip().upper() in VALID]
    pending = [r for r in rows if r not in annotated]
    unannotated = [r for r in rows if (r.get("expected") or "").strip() == ""]

    correct = [r for r in annotated if r["expected"].strip().upper() == "TRUE"]
    wrong = [r for r in annotated if r["expected"].strip().upper() == "FALSE"]
    na = [r for r in annotated if r["expected"].strip().upper() == "N/A"]

    accuracy = len(correct) / len(annotated) if annotated else 0.0

    # 按关系类型分层
    by_rel = defaultdict(lambda: {"total": 0, "true": 0, "false": 0, "na": 0})
    for r in rows:
        key = r.get("rel_type") or "unknown"
        by_rel[key]["total"] += 1
    for r in correct:
        by_rel[r.get("rel_type") or "unknown"]["true"] += 1
    for r in wrong:
        by_rel[r.get("rel_type") or "unknown"]["false"] += 1
    for r in na:
        by_rel[r.get("rel_type") or "unknown"]["na"] += 1

    verifiers = Counter(r.get("verifier", "").strip() for r in annotated if r.get("verifier"))

    lines = []
    lines.append("# 关系抽检人工标注统计报告")
    lines.append("")
    lines.append(f"生成时间：{datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 样本总数：**{total}** 条")
    lines.append(f"- 已标注：**{len(annotated)}** 条（{len(annotated)/total*100:.1f}%）")
    lines.append(f"- 未标注：**{len(unannotated)}** 条（待人工回填 expected 列）")
    lines.append(f"- 判定成立（TRUE）：{len(correct)} 条")
    lines.append(f"- 判定不成立（FALSE）：{len(wrong)} 条")
    lines.append(f"- 不适用（N/A）：{len(na)} 条")
    if annotated:
        lines.append(f"- **关系准确率：{accuracy*100:.1f}%**（{len(correct)}/{len(annotated)}）")
    else:
        lines.append("- **关系准确率：未标注，无法计算**")
    if verifiers:
        lines.append(f"- 标注人：{'、'.join(f'{k}（{v}条）' for k, v in verifiers.most_common())}")
    lines.append("")
    lines.append("## 分层准确率")
    lines.append("")
    lines.append("| 关系类型 | 样本数 | TRUE | FALSE | N/A | 准确率 |")
    lines.append("|---|---|---|---|---|---|")
    for rel, c in sorted(by_rel.items()):
        done = c["true"] + c["false"]
        rate = c["true"] / done * 100 if done else 0.0
        lines.append(f"| {rel} | {c['total']} | {c['true']} | {c['false']} | {c['na']} | {rate:.1f}% |")
    lines.append("")
    lines.append("## 未标注明细（待人工回填）")
    lines.append("")
    if unannotated:
        lines.append("| 关系类型 | 源实体 | 目标实体 |")
        lines.append("|---|---|---|")
        for r in unannotated:
            lines.append(f"| {r.get('rel_type') or ''} | {r.get('source') or ''} | {r.get('target') or ''} |")
    else:
        lines.append("全部样本已标注。")
    lines.append("")
    lines.append("## 标注说明")
    lines.append("")
    lines.append("- TRUE = 该关系的源实体/目标实体/类型/方向与原始证据一致")
    lines.append("- FALSE = 不一致（关系错误、方向反或实体不匹配）")
    lines.append("- N/A = 无法核验（证据缺失）")
    lines.append("- 标注规则详见 `sampling_review_instructions.md`")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"✓ sampling_accuracy_report.md（已标注 {len(annotated)}/{total}，准确率 {accuracy*100:.1f}%）")


if __name__ == "__main__":
    main()
