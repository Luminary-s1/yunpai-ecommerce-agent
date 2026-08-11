"""07_sampling.py — 生成真值表 + 关系抽检样本（对齐计划 §6.3 / §7）。

产出（06_report/）：
  truth_table.csv    真值表：核心实体全量（覆盖率分母 40）预期值
  sampling_plan.csv  关系抽检计划：核心池 60 条（分层随机）+ 扩展池 20 条

对齐硬性要求：
  - §6.3 核心实体 = SPU(8) + SKU(12) + 品类(10) + 政策(≈10)，分母 40
  - §7.2 核心池分层：BELONGS_TO 10 / HAS_ATTR 10 / APPLIES_TO 14 / REFERS_TO 14 / RELATED_TO 12
  - §7.2 随机种子 random.Random(20260803)，可复现
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

CLEAN_ROOT = Path(__file__).resolve().parent.parent / "02_clean"
REPORT_ROOT = Path(__file__).resolve().parent.parent / "06_report"

# 分层抽样配置（§7.2）
LAYER_SIZES = {"belongs_to": 10, "has_attr": 10, "applies_to": 14, "refers_to": 14, "related_to": 12}


def load_json(name: str) -> list[dict]:
    return json.loads((CLEAN_ROOT / name).read_text(encoding="utf-8"))


def gen_truth_table() -> None:
    """§6.3 真值表：核心实体全量（分母 40）。"""
    products = load_json("product.json")
    categories = load_json("category.json")
    policies = load_json("policy.json")

    rows = []
    # 商品 SPU（8）
    seen_spu = set()
    for p in products:
        if p["item_id"] not in seen_spu:
            seen_spu.add(p["item_id"])
            rows.append({
                "doc_id": p["source"], "entity_key": p["item_id"], "entity_type": "Product(SPU)",
                "relation_key": "", "relation_type": "",
                "expected_value": f"SPU {p['item_id']}（{p['title']}，价格 {p['sale_price']}）",
            })
    # 商品 SKU（12）
    for p in products:
        rows.append({
            "doc_id": p["source"], "entity_key": p["sku_id"], "entity_type": "SKU",
            "relation_key": "", "relation_type": "",
            "expected_value": f"SKU {p['sku_id']}（归属 {p['item_id']}，品类 {p['category_name']}）",
        })
    # 品类（10）
    for c in categories:
        rows.append({
            "doc_id": "catalog", "entity_key": c["category_code"], "entity_type": "Category",
            "relation_key": "", "relation_type": "",
            "expected_value": f"品类 {c['category_code']}（{c['category_name']}，父级 {c['parent_category'] or '无'}）",
        })
    # 售后政策（≈10，含扩展）
    for pol in policies:
        rows.append({
            "doc_id": pol["source"], "entity_key": pol["policy_code"], "entity_type": "Policy",
            "relation_key": "", "relation_type": "",
            "expected_value": f"政策 {pol['policy_code']}（{pol['policy_type']}，{pol['policy_name']}）",
        })

    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    with open(REPORT_ROOT / "truth_table.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["doc_id", "entity_key", "entity_type", "relation_key", "relation_type", "expected_value"])
        writer.writeheader()
        writer.writerows(rows)

    # 核心实体计数（分母 40：SPU 8 + SKU 12 + 品类 10 + 政策 10）
    spu = len({p["item_id"] for p in products})
    sku = len(products)
    cat = len(categories)
    pol = len(policies)
    print(f"✓ truth_table.csv（{len(rows)} 行）")
    print(f"  核心实体分母: SPU {spu} + SKU {sku} + 品类 {cat} + 政策 {pol} = {spu+sku+cat+pol}")


def gen_sampling_plan() -> None:
    """§7.2 关系抽检计划：核心池 60 条分层随机。"""
    rng = random.Random(20260803)  # 固定种子，可复现
    plans = []
    total = 0
    for rel_name, n in LAYER_SIZES.items():
        rels = load_json(f"{rel_name}.json")
        sample = rng.sample(rels, min(n, len(rels)))
        for r in sample:
            plans.append({
                "round": 1,
                "rel_type": r["rel_type"],
                "source": r["source"],
                "target": r["target"],
                "expected": "TRUE",  # 抽检判定标准：头尾实体+类型+方向全对
                "confidence": r.get("confidence", ""),
                "generated_by": r.get("generated_by", ""),
            })
            total += 1
    with open(REPORT_ROOT / "sampling_plan.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["round", "rel_type", "source", "target", "expected", "confidence", "generated_by"])
        writer.writeheader()
        writer.writerows(plans)
    print(f"✓ sampling_plan.csv（核心池 {total} 条）")


def main() -> None:
    gen_truth_table()
    gen_sampling_plan()


if __name__ == "__main__":
    main()
