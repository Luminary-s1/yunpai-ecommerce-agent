# -*- coding: utf-8 -*-
"""11_refresh_manifest.py — 重写 clean_manifest.json 到实际落盘计数（D-035 单一事实源）。

问题背景（验收 P1-6）：
- 根目录 02_clean/clean_manifest.json 记录 faq 60 / rule 9（Aug 3），实际 faq 63 / rule 17（Aug 7 扩充后）
- 缺 sku 计数（sku.json 由 product.json 派生）
- 关系计数停留在扩充前（12/51/34/64/52），实际 19/51/36/65/69

本脚本读 02_clean 实际 JSON 计数 + 关系文件计数，重写 clean_manifest.json。
用法：
  python 05_scripts/11_refresh_manifest.py [--target 02_clean]
  --target 可传 02_clean 或 07_handoff/02_clean（默认 02_clean）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 实体 JSON → manifest 计数键（含扩展文件）
ENTITY_FILES: list[tuple[str, str]] = [
    ("category.json", "category"),
    ("product.json", "product"),
    ("sku.json", "sku"),
    ("attribute.json", "attribute"),
    ("policy.json", "policy"),
    ("script.json", "script"),
    ("faq.json", "faq"),
    ("rule.json", "rule"),
    ("rule_extended.json", "rule_extended"),
]
# 关系 JSON → manifest 计数键
REL_FILES: list[tuple[str, str]] = [
    ("belongs_to.json", "belongs_to"),
    ("has_attr.json", "has_attr"),
    ("applies_to.json", "applies_to"),
    ("refers_to.json", "refers_to"),
    ("related_to.json", "related_to"),
]


def refresh_manifest(target: str) -> None:
    clean = ROOT / target
    if not clean.is_dir():
        raise SystemExit(f"目录不存在: {clean}")

    entities: dict[str, int] = {}
    # product.json 每行是一个 SKU；SPU 按 item_id 去重，SKU 以 sku.json 为准
    product_path = clean / "product.json"
    if product_path.exists():
        products = json.loads(product_path.read_text(encoding="utf-8"))
        entities["product"] = len({p["item_id"] for p in products})
    else:
        entities["product"] = 0
    sku_path = clean / "sku.json"
    if sku_path.exists():
        entities["sku"] = len(json.loads(sku_path.read_text(encoding="utf-8")))
    else:
        entities["sku"] = len({p["sku_id"] for p in products}) if product_path.exists() else 0
    for fname, key in ENTITY_FILES:
        if key in ("product", "sku"):
            continue
        path = clean / fname
        if path.exists():
            entities[key] = len(json.loads(path.read_text(encoding="utf-8")))
        else:
            entities[key] = 0

    relationships: dict[str, int] = {}
    for fname, key in REL_FILES:
        path = clean / fname
        if path.exists():
            relationships[key] = len(json.loads(path.read_text(encoding="utf-8")))
        else:
            relationships[key] = 0

    manifest = {
        "schema_version": 1,
        "generated_at": "2026-08-11",
        "regenerated_by": "11_refresh_manifest.py",
        "entities": entities,
        "relationships": relationships,
        "total_nodes": sum(entities.values()),
        "total_rels": sum(relationships.values()),
    }
    out = clean / "clean_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✓ 已重写 {out}")
    print(f"  实体: {entities}")
    print(f"  关系: {relationships}")
    print(f"  合计: {manifest['total_nodes']} 节点 / {manifest['total_rels']} 关系")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="02_clean", help="目标 02_clean 目录（相对 knowledge_graph_output）")
    args = parser.parse_args()
    refresh_manifest(args.target)
