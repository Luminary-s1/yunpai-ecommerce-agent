# -*- coding: utf-8 -*-
"""verify_single_source.py — 交付包一致性校验（D-035 单一事实源）。

校验内容：
1. 根目录 vs 07_handoff 关键文件 md5 一致（manifest/stats/数据/导入文件）
2. 导入 CSV 唯一键无重复（nodes_*.csv 的 id 列）
3. 关系 CSV 端点 0 悬空（rels_*.csv 的 source/target 都能解析到节点）

用法：
  python 05_scripts/verify_single_source.py
退出码：0=通过，1=不通过
"""
from __future__ import annotations

import csv
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "07_handoff"

# 需要一致的关键文件（根 vs handoff 相对路径）
KEY_PAIRS: list[tuple[str, str]] = [
    ("02_clean/clean_manifest.json", "02_clean/clean_manifest.json"),
    ("02_clean/faq.json", "02_clean/faq.json"),
    ("02_clean/policy.json", "02_clean/policy.json"),
    ("02_clean/sku.json", "02_clean/sku.json"),
    ("06_report/graph_stats.json", "06_report/graph_stats.json"),
    ("06_report/sampling_plan.csv", "06_report/sampling_plan.csv"),
    ("01_raw/manual/new_faqs.json", "01_raw/manual/new_faqs.json"),
    ("04_import/nodes_faq.csv", "04_import/nodes_faq.csv"),
    ("04_import/nodes_policy.csv", "04_import/nodes_policy.csv"),
    ("04_import/nodes_rule.csv", "04_import/nodes_rule.csv"),
    ("04_import/rels_refers_to.csv", "04_import/rels_refers_to.csv"),
]

# 节点 CSV → 唯一键列
NODE_KEY_COLS: dict[str, str] = {
    "nodes_category.csv": "category_code",
    "nodes_product.csv": "item_id",
    "nodes_sku.csv": "sku_id",
    "nodes_attribute.csv": "spec_key",
    "nodes_policy.csv": "policy_code",
    "nodes_script.csv": "script_id",
    "nodes_faq.csv": "faq_id",
    "nodes_rule.csv": "rule_code",
}
# 关系 CSV → (source 列, target 列, source 前缀标签, target 前缀标签)
REL_FILES: dict[str, tuple[str, str]] = {
    "rels_belongs_to.csv": ("source", "target"),
    "rels_has_attr.csv": ("source", "target"),
    "rels_applies_to.csv": ("source", "target"),
    "rels_refers_to.csv": ("source", "target"),
    "rels_related_to.csv": ("source", "target"),
}


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def check_consistency() -> list[str]:
    errors: list[str] = []
    for rp, hp in KEY_PAIRS:
        r, h = ROOT / rp, HANDOFF / hp
        if not r.exists() or not h.exists():
            errors.append(f"缺失: {rp} (root={r.exists()}, handoff={h.exists()})")
        elif md5(r) != md5(h):
            errors.append(f"md5 不一致: {rp}")
    return errors


def check_unique_keys() -> list[str]:
    errors: list[str] = []
    import_dir = ROOT / "04_import"
    for fname, key_col in NODE_KEY_COLS.items():
        path = import_dir / fname
        if not path.exists():
            continue
        seen: set[str] = set()
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                k = row.get(key_col, "")
                if k in seen:
                    errors.append(f"唯一键重复: {fname} {key_col}={k}")
                seen.add(k)
    return errors


def check_dangling() -> list[str]:
    """关系 CSV 的 source/target 都能解析到节点 id。"""
    errors: list[str] = []
    import_dir = ROOT / "04_import"
    node_ids: set[str] = set()
    for fname, key_col in NODE_KEY_COLS.items():
        path = import_dir / fname
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    node_ids.add(row.get(key_col, ""))
    for fname, (src_col, tgt_col) in REL_FILES.items():
        path = import_dir / fname
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get(src_col, "") not in node_ids:
                    errors.append(f"悬空 source: {fname} {src_col}={row.get(src_col)}")
                if row.get(tgt_col, "") not in node_ids:
                    errors.append(f"悬空 target: {fname} {tgt_col}={row.get(tgt_col)}")
    return errors


def main() -> int:
    errors: list[str] = []
    errors += check_consistency()
    errors += check_unique_keys()
    errors += check_dangling()
    if errors:
        print(f"❌ 校验不通过（{len(errors)} 项）：")
        for e in errors[:30]:
            print(f"  - {e}")
        if len(errors) > 30:
            print(f"  ... 共 {len(errors)} 项")
        return 1
    print(f"✅ 校验通过：根目录 vs 07_handoff 一致（{len(KEY_PAIRS)} 项 md5），唯一键无重复，关系 0 悬空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
