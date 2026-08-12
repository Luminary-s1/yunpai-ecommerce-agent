"""06_export.py — 生成下游对齐契约 + Neo4j 导入文件。

产出：
  03_dictionary/dictionary_schema.json  机器可读唯一契约（§2.4）
  04_import/nodes_*.csv                 每节点类型一个 CSV
  04_import/rels_*.csv                  每关系类型一个 CSV
  04_import/00_setup.cypher             唯一约束 ×15
  04_import/01_load_nodes.cypher        MERGE + SET 导入节点
  04_import/02_load_rels.cypher         MATCH 两端 + MERGE 导入关系

对齐计划 §8.1：所有 CSV 带 updated_at 字段（增量导入机制）；UTF-8 无 BOM。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

CLEAN_ROOT = Path(__file__).resolve().parent.parent / "02_clean"
DICT_ROOT = Path(__file__).resolve().parent.parent / "03_dictionary"
IMPORT_ROOT = Path(__file__).resolve().parent.parent / "04_import"

TODAY = datetime.now().strftime("%Y-%m-%d")

# 实体类型 → (Neo4j label, 唯一键属性, 需导出的字段)
ENTITY_SCHEMA: dict[str, tuple[str, str, list[str]]] = {
    "category": ("Category", "category_code", ["category_code", "category_name", "parent_category", "level"]),
    "product": ("Product", "item_id", ["item_id", "title", "model", "status", "sale_price", "warranty_months", "category", "category_name", "source"]),
    "sku": ("SKU", "sku_id", ["sku_id", "item_id", "title", "color", "status", "sale_price", "category", "source"]),
    "attribute": ("Attribute", "spec_key", ["spec_key", "attr_key", "attr_value", "level", "owner_id"]),
    "policy": ("Policy", "policy_code", ["policy_code", "policy_type", "policy_name", "content", "scope", "scope_key", "risk_level", "source"]),
    "script": ("Script", "script_id", ["script_id", "category", "intent", "keywords", "canonical_answer", "risk_level", "layer", "source"]),
    "faq": ("FAQ", "faq_id", ["faq_id", "category", "intent", "question", "answer", "risk_level", "layer", "ref_script_id", "sku_id", "source"]),
    "rule": ("Rule", "rule_code", ["rule_code", "rule_title", "authority", "theme", "content_summary", "source", "source_url", "captured_at"]),
}

# 关系类型 → (Neo4j rel type, 关系属性)
# 端点统一用通用 id 匹配（01_load_nodes 中已 SET n.id = 唯一键）
REL_SCHEMA: dict[str, tuple[str, list[str]]] = {
    "belongs_to": ("BELONGS_TO", ["confidence", "generated_by"]),
    "has_attr": ("HAS_ATTR", ["confidence", "generated_by"]),
    "applies_to": ("APPLIES_TO", ["confidence", "generated_by"]),
    "refers_to": ("REFERS_TO", ["confidence", "generated_by", "target_type"]),
    "related_to": ("RELATED_TO", ["confidence", "generated_by"]),
}


def load_json(name: str) -> list[dict]:
    return json.loads((CLEAN_ROOT / name).read_text(encoding="utf-8"))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """UTF-8 无 BOM 写 CSV。"""
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            row = {k: (v if v is not None else "") for k, v in r.items()}
            writer.writerow(row)


def export_entities() -> None:
    IMPORT_ROOT.mkdir(parents=True, exist_ok=True)
    # product.json 拆分：SPU 节点（去重按 item_id）+ SKU 节点（每个 SKU 一行）
    products = load_json("product.json")
    spu_rows = []
    sku_rows = []
    seen_spu: set[str] = set()
    for p in products:
        if p["item_id"] not in seen_spu:
            seen_spu.add(p["item_id"])
            spu_rows.append({
                "item_id": p["item_id"],
                "title": p["title"],
                "model": p.get("model", ""),
                "status": p.get("status", "active"),
                "sale_price": p.get("sale_price", ""),
                "warranty_months": p.get("warranty_months", 0),
                "category": p.get("category", ""),
                "category_name": p.get("category_name", ""),
                "source": p.get("source", "fixture"),
            })
        sku_attrs = p.get("sku_attributes", {})
        sku_rows.append({
            "sku_id": p["sku_id"],
            "item_id": p["item_id"],
            "title": p["title"],
            "color": sku_attrs.get("color", ""),
            "status": p.get("status", "active"),
            "sale_price": p.get("sale_price", ""),
            "category": p.get("category", ""),
            "source": p.get("source", "fixture"),
        })

    # 写 SPU + SKU（覆盖 product 的默认导出）
    for name, rows in [("product", spu_rows), ("sku", sku_rows)]:
        label, uid, fields = ENTITY_SCHEMA[name]
        out_fields = fields + ["updated_at"]
        for r in rows:
            r["updated_at"] = TODAY
        write_csv(IMPORT_ROOT / f"nodes_{name}.csv", out_fields, rows)
        print(f"✓ nodes_{name}.csv（{len(rows)} 行）")

    # 其余实体按 schema 导出
    for name, (label, uid, fields) in ENTITY_SCHEMA.items():
        if name in ("product", "sku"):
            continue
        if name == "rule":
            # rule 节点 = rule.json（9 条）+ rule_extended.json（8 条）= 17 条
            rows = load_json("rule.json") + load_json("rule_extended.json")
        else:
            rows = load_json(f"{name}.json")
        out_fields = fields + ["updated_at"]
        for r in rows:
            r["updated_at"] = TODAY
        write_csv(IMPORT_ROOT / f"nodes_{name}.csv", out_fields, rows)
        print(f"✓ nodes_{name}.csv（{len(rows)} 行）")


def export_relationships() -> None:
    for name, (rel_type, rel_fields) in REL_SCHEMA.items():
        rows = load_json(f"{name}.json")
        out_fields = ["source", "target", "rel_type", "updated_at"] + rel_fields
        for r in rows:
            r["updated_at"] = TODAY
        write_csv(IMPORT_ROOT / f"rels_{name}.csv", out_fields, rows)
        print(f"✓ rels_{name}.csv（{len(rows)} 行）")


def gen_dictionary_schema() -> None:
    """§2.4 机器可读唯一契约。"""
    schema = {
        "schema_version": "1.0",
        "updated_at": TODAY,
        "node_labels": {name: {"label": label, "unique_key": uid, "fields": fields}
                        for name, (label, uid, fields) in ENTITY_SCHEMA.items()},
        "relationship_types": {name: {"type": rel_type, "fields": rel_fields}
                               for name, (rel_type, rel_fields) in REL_SCHEMA.items()},
        "enums": {
            "category_code": ["home_appliance", "air_fryer", "cordless_vacuum", "humidifier",
                              "electric_kettle", "air_circulation_fan", "digital", "digital_audio",
                              "digital_power", "apparel"],
            "policy_type": ["warranty", "return", "refund", "exchange", "logistics", "invoice", "price_protection"],
            "risk_level": ["low", "medium", "high", "critical"],
            "layer": ["platform", "industry", "store", "product", "evolution"],
            "status": ["active", "inactive", "retired"],
            "attr_key": ["brand", "model", "warranty_months", "color", "capacity_l", "runtime_min",
                         "filter", "size", "material", "season", "battery_mah", "storage_gb"],
        },
    }
    DICT_ROOT.mkdir(parents=True, exist_ok=True)
    (DICT_ROOT / "dictionary_schema.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("✓ dictionary_schema.json")


def gen_cypher() -> None:
    """生成 00_setup / 01_load_nodes / 02_load_rels。"""
    # 唯一约束（每实体 1 个 + 属性 1 个 + 政策 1 个）
    setup = ["// 00_setup.cypher — 唯一约束与索引（幂等，可重跑）"]
    for name, (label, uid, _) in ENTITY_SCHEMA.items():
        setup.append(f"CREATE CONSTRAINT unique_{label} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{uid} IS UNIQUE;")
    setup.append("CREATE INDEX idx_attr_key IF NOT EXISTS FOR (a:Attribute) ON (a.attr_key);")
    setup.append("CREATE INDEX idx_script_intent IF NOT EXISTS FOR (s:Script) ON (s.intent);")
    setup.append("CREATE INDEX idx_faq_intent IF NOT EXISTS FOR (f:FAQ) ON (f.intent);")
    (IMPORT_ROOT / "00_setup.cypher").write_text("\n".join(setup) + "\n", encoding="utf-8")

    # 加载节点（MERGE + SET），同时 SET 通用 id 供关系端点匹配
    load_nodes = ["// 01_load_nodes.cypher — 加载节点（MERGE 幂等，SET 通用 id）"]
    for name, (label, uid, fields) in ENTITY_SCHEMA.items():
        set_fields = ",\n  ".join(f"n.{f} = row.{f}" for f in fields if f != uid)
        load_nodes.append(
            f"LOAD CSV WITH HEADERS FROM 'file:///kg/nodes_{name}.csv' AS row\n"
            f"MERGE (n:{label} {{{uid}: row.{uid}}})\n"
            f"SET n.id = row.{uid},\n  n.updated_at = row.updated_at,\n  {set_fields};"
        )
    (IMPORT_ROOT / "01_load_nodes.cypher").write_text("\n".join(load_nodes) + "\n", encoding="utf-8")

    # 加载关系（按通用 id 匹配两端 + MERGE）
    load_rels = ["// 02_load_rels.cypher — 加载关系（按通用 id 匹配两端 + MERGE）"]
    for name, (rel_type, rel_fields) in REL_SCHEMA.items():
        set_rel = ", ".join(f"r.{f} = row.{f}" for f in rel_fields)
        load_rels.append(
            f"LOAD CSV WITH HEADERS FROM 'file:///kg/rels_{name}.csv' AS row\n"
            f"MATCH (a {{id: row.source}}), (b {{id: row.target}})\n"
            f"MERGE (a)-[r:{rel_type}]->(b)\n"
            f"SET r.updated_at = row.updated_at, {set_rel};"
        )
    (IMPORT_ROOT / "02_load_rels.cypher").write_text("\n".join(load_rels) + "\n", encoding="utf-8")

    print("✓ 00_setup.cypher / 01_load_nodes.cypher / 02_load_rels.cypher")


def cleanup_legacy_files() -> None:
    """清理历史遗留导入文件（D-035：单一事实源，负责人二次 review #8）。

    - nodes_rule_extended.csv 是早期未合并版本的残留：rule 已合并进
      nodes_rule.csv（rule.json 9 条 + rule_extended.json 8 条 = 17 条），
      单独保留扩展文件会误导二次导入（Cypher 只载 nodes_rule.csv）。
    """
    legacy = [
        IMPORT_ROOT / "nodes_rule_extended.csv",
    ]
    for f in legacy:
        if f.exists():
            f.unlink()
            print(f"✓ 清理遗留文件 {f.name}")


def main() -> None:
    cleanup_legacy_files()
    gen_dictionary_schema()
    export_entities()
    export_relationships()
    gen_cypher()


if __name__ == "__main__":
    main()
