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
    """§6.3 真值表：核心实体全量 + 知识主体（FAQ/Script/Rule）。

    对齐负责人二次 review #3（覆盖率分母不得只含商品/品类/政策）：
    - 分母 = SPU + SKU + 品类 + 政策 + FAQ + 话术 + 规则（全部实体类型）
    - 每类都从 02_clean 实际读取，保证与交付数据一致
    """
    products = load_json("product.json")
    categories = load_json("category.json")
    policies = load_json("policy.json")
    faqs = load_json("faq.json")
    scripts = load_json("script.json")
    rules = load_json("rule.json")
    rules_ext = load_json("rule_extended.json")

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
    # FAQ（63，知识主体）
    for f in faqs:
        rows.append({
            "doc_id": f.get("source", ""), "entity_key": f.get("faq_id", ""), "entity_type": "FAQ",
            "relation_key": "", "relation_type": "",
            "expected_value": f"FAQ {f.get('faq_id', '')}（{f.get('question', '')[:60]}）",
        })
    # 客服话术（52，知识主体）
    for s in scripts:
        rows.append({
            "doc_id": s.get("source", ""), "entity_key": s.get("script_id", ""), "entity_type": "Script",
            "relation_key": "", "relation_type": "",
            "expected_value": f"话术 {s.get('script_id', '')}（{s.get('title', s.get('category', ''))[:60]}）",
        })
    # 行业规则（9 + 扩展 8 = 17，知识主体）
    all_rules = rules + rules_ext
    for r in all_rules:
        rows.append({
            "doc_id": r.get("source", ""), "entity_key": r.get("rule_code", ""), "entity_type": "Rule",
            "relation_key": "", "relation_type": "",
            "expected_value": f"规则 {r.get('rule_code', '')}（{r.get('rule_title', '')[:60]}）",
        })

    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    with open(REPORT_ROOT / "truth_table.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["doc_id", "entity_key", "entity_type", "relation_key", "relation_type", "expected_value"])
        writer.writeheader()
        writer.writerows(rows)

    # 覆盖分母（全部实体类型，不再是只有 SPU/SKU/品类/政策）
    spu = len({p["item_id"] for p in products})
    sku = len(products)
    cat = len(categories)
    pol = len(policies)
    faq = len(faqs)
    script = len(scripts)
    rule = len(all_rules)
    total = spu + sku + cat + pol + faq + script + rule
    print(f"✓ truth_table.csv（{len(rows)} 行）")
    print(f"  覆盖分母: SPU {spu} + SKU {sku} + 品类 {cat} + 政策 {pol} + FAQ {faq} + 话术 {script} + 规则 {rule} = {total}")


def gen_sampling_plan() -> None:
    """§7.2 关系抽检计划：核心池 60 条分层随机（人工可复核标注模板）。

    验收要求"人工抽检"（validation_report 注明"人工抽检比对"）：
    - expected 列**留空待人工填**（TRUE=头尾实体+类型+方向与证据一致；FALSE=不一致）
    - 新增 evidence 列：从端点实体 JSON 拼出原始证据引用（source_url/source），
      让人能回到 01_raw 核验，不再"自取自答"
    - annotation/verifier/verified_at 列：标注结论、标注人、标注时间
    - 抽样逻辑不变：分层随机 + 固定种子可复现
    """
    rng = random.Random(20260803)  # 固定种子，可复现
    plans = []
    total = 0
    # 端点实体索引：source/target id → 记录（拼 evidence 用）
    entity_index: dict[str, dict] = {}
    for fname in (
        "faq.json", "policy.json", "script.json", "rule.json", "rule_extended.json",
        "product.json", "category.json", "attribute.json",
    ):
        for rec in load_json(fname):
            eid = (
                rec.get("faq_id") or rec.get("policy_code") or rec.get("script_id")
                or rec.get("rule_code") or rec.get("item_id") or rec.get("category_code")
                or rec.get("spec_key") or rec.get("id")
            )
            if eid:
                entity_index[str(eid)] = rec
    for rel_name, n in LAYER_SIZES.items():
        rels = load_json(f"{rel_name}.json")
        sample = rng.sample(rels, min(n, len(rels)))
        for r in sample:
            src = entity_index.get(str(r["source"]), {})
            tgt = entity_index.get(str(r["target"]), {})
            evidence = " | ".join(
                filter(
                    None,
                    [
                        src.get("source_url") or src.get("source") or "",
                        src.get("policy_name") or src.get("question") or src.get("rule_title") or "",
                        tgt.get("source_url") or tgt.get("source") or "",
                        tgt.get("policy_name") or tgt.get("rule_title") or "",
                    ],
                )
            )
            plans.append({
                "round": 1,
                "rel_type": r["rel_type"],
                "source": r["source"],
                "target": r["target"],
                "expected": "",  # 人工标注：TRUE / FALSE（留空待填）
                "evidence": evidence,
                "annotation": "",
                "verifier": "",
                "verified_at": "",
                "confidence": r.get("confidence", ""),
                "generated_by": r.get("generated_by", ""),
            })
            total += 1
    with open(REPORT_ROOT / "sampling_plan.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "round", "rel_type", "source", "target", "expected",
                "evidence", "annotation", "verifier", "verified_at",
                "confidence", "generated_by",
            ],
        )
        writer.writeheader()
        writer.writerows(plans)
    print(f"✓ sampling_plan.csv（核心池 {total} 条，expected 待人工标注）")


def main() -> None:
    gen_truth_table()
    gen_sampling_plan()


if __name__ == "__main__":
    main()
