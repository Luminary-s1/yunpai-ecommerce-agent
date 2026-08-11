"""01_extract_local.py — 提取本地源数据（L0 种子 + L1 知识源）为带来源标记的中间清单。

输出到 ../01_raw/：
  seed/      virtual_store_v1.json 解析出的 商品/政策/FAQ/订单 素材（含 source=fixture）
  knowledge/ knowledge_seed.py 的 43 条 SOP 清单（含 source=builtin:ecommerce-sop-v1）

只做"提取 + 标记 + 落盘"，不做清洗（清洗在 02 阶段）。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

# 脚本位于 <项目根>/knowledge_graph_output/05_scripts/，往上一级是项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
SEED_PATH = SRC_ROOT / "ecommerce_agent" / "fixtures" / "virtual_store_v1.json"

if SRC_ROOT not in [str(p) for p in sys.path]:
    sys.path.insert(0, str(SRC_ROOT))

from ecommerce_agent.knowledge_seed import TOPICS, seed_records  # noqa: E402
OUT_ROOT = Path(__file__).resolve().parent.parent / "01_raw"

SOURCE_FIXTURE = "fixture"
SOURCE_SOP = "builtin:ecommerce-sop-v1"


def _load_seed() -> dict:
    with open(SEED_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_catalog(seed: dict) -> list[dict]:
    """catalog 行 → 商品素材。保留 item_id/sku_id 分组关系与全部属性（SPU 级 / SKU 级划分在 02 清洗做）。"""
    rows: list[dict] = []
    for sku in seed.get("catalog", []):
        rows.append(
            {
                "item_id": sku["item_id"],
                "sku_id": sku["sku_id"],
                "title": sku["title"],
                "status": sku.get("status"),
                "sale_price": sku.get("sale_price"),
                "attributes": sku.get("attributes", {}),
                "source": SOURCE_FIXTURE,
            }
        )
    return rows


def extract_policies(seed: dict) -> list[dict]:
    """catalog 的 warranty_months → 保修素材（每条 SKU 一条，聚合动作在 02 清洗做）。"""
    rows: list[dict] = []
    for sku in seed.get("catalog", []):
        wm = sku.get("attributes", {}).get("warranty_months")
        if wm is not None:
            rows.append(
                {
                    "sku_id": sku["sku_id"],
                    "policy_type": "warranty",
                    "content": f"整机保修 {wm} 个月，保修以订单和产品序列信息核验结果为准。",
                    "scope": "SKU",
                    "scope_key": sku["sku_id"],
                    "warranty_months": wm,
                    "source": SOURCE_FIXTURE,
                }
            )
    return rows


def extract_faqs(seed: dict) -> list[dict]:
    """seed.knowledge 的 4 条 → FAQ 素材。"""
    rows: list[dict] = []
    for item in seed.get("knowledge", []):
        rows.append(
            {
                "category": item.get("category"),
                "intent": item.get("intent"),
                "question": item.get("question"),
                "answer": item.get("answer"),
                "keywords": item.get("keywords"),
                "risk_level": item.get("risk_level"),
                "layer": item.get("layer"),
                "sku_id": item.get("sku_id"),
                "source": SOURCE_FIXTURE,
            }
        )
    return rows


def extract_orders(seed: dict) -> list[dict]:
    """orders → 订单/物流/售后 素材（供运营与溯源场景，属扩展数据）。"""
    return [o for o in seed.get("orders", []) if o.get("order_id")]


def extract_scripts() -> list[dict]:
    """knowledge_seed.TOPICS 43 条 → 话术素材（canonical_answer + questions 变体）。"""
    rows: list[dict] = []
    for idx, t in enumerate(TOPICS, start=1):
        rows.append(
            {
                "script_id": f"SCRIPT-{idx:03d}",
                "category": t.get("category"),
                "intent": t.get("intent"),
                "keywords": t.get("keywords"),
                "canonical_answer": t.get("answer"),
                "questions": t.get("questions", []),
                "risk_level": "low",
                "layer": "store",
                "source": SOURCE_SOP,
            }
        )
    return rows


def main() -> None:
    seed = _load_seed()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    # L0 种子
    catalog = extract_catalog(seed)
    policies = extract_policies(seed)
    faqs = extract_faqs(seed)
    orders = extract_orders(seed)

    # L1 知识源
    scripts = extract_scripts()

    (OUT_ROOT / "seed").mkdir(exist_ok=True)
    (OUT_ROOT / "knowledge").mkdir(exist_ok=True)

    (OUT_ROOT / "seed" / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_ROOT / "seed" / "policies.json").write_text(
        json.dumps(policies, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_ROOT / "seed" / "faqs.json").write_text(
        json.dumps(faqs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_ROOT / "seed" / "orders.json").write_text(
        json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_ROOT / "knowledge" / "scripts.json").write_text(
        json.dumps(scripts, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 落一份清单 manifest
    manifest = {
        "source_label": {
            "L0": SOURCE_FIXTURE,
            "L1": SOURCE_SOP,
        },
        "counts": {
            "catalog_skus": len(catalog),
            "catalog_items": len({r["item_id"] for r in catalog}),
            "policies": len(policies),
            "seed_faqs": len(faqs),
            "orders": len(orders),
            "scripts": len(scripts),
            "sop_questions": sum(len(s["questions"]) for s in scripts),
        },
    }
    (OUT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
