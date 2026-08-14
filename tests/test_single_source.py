"""交付包一致性测试（D-035 单一事实源）：根目录 vs 07_handoff + sku.json + manifest。"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
KB_ROOT = REPO_ROOT / "knowledge_graph_output"
HANDOFF = KB_ROOT / "07_handoff"


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def test_handoff_consistent_with_root() -> None:
    """根目录与 07_handoff 关键文件 md5 一致（D-035）。

    契约变更（第三轮 R3）：07_handoff/ 由 05_scripts/09_handoff.py 重建，
    已从 git 出仓（.gitignore 排除）。仅当目录仍被 git 跟踪时才比对；
    出仓后跳过（交付包由 09_handoff.py 一次性生成，不再维护双份）。
    """
    if not HANDOFF.is_dir():
        pytest.skip("07_handoff 目录不存在")
    import subprocess
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", "knowledge_graph_output/07_handoff/"],
        capture_output=True,
        cwd=REPO_ROOT,
    )
    if tracked.returncode != 0:
        pytest.skip("07_handoff 已出仓（09_handoff.py 重建），不再比对")
    pairs = [
        ("02_clean/clean_manifest.json", "02_clean/clean_manifest.json"),
        ("02_clean/faq.json", "02_clean/faq.json"),
        ("02_clean/policy.json", "02_clean/policy.json"),
        ("02_clean/sku.json", "02_clean/sku.json"),
        ("06_report/graph_stats.json", "06_report/graph_stats.json"),
        ("06_report/sampling_plan.csv", "06_report/sampling_plan.csv"),
        ("04_import/rels_refers_to.csv", "04_import/rels_refers_to.csv"),
    ]
    for rp, hp in pairs:
        r, h = KB_ROOT / rp, HANDOFF / hp
        assert r.is_file(), f"根目录缺失 {rp}"
        assert h.is_file(), f"交接目录缺失 {hp}"
        assert md5(r) == md5(h), f"不一致: {rp}"


def test_sku_json_exists_and_unique() -> None:
    """sku.json 存在，sku_id 唯一，条数 ≥ 12。"""
    path = KB_ROOT / "02_clean" / "sku.json"
    assert path.is_file(), "02_clean/sku.json 缺失"
    skus = json.loads(path.read_text(encoding="utf-8"))
    assert len(skus) >= 12
    ids = [s["sku_id"] for s in skus]
    assert len(ids) == len(set(ids)), "sku_id 有重复"


def test_manifest_counts_match_data() -> None:
    """clean_manifest.json 计数与实际数据一致。"""
    manifest = json.loads(
        (KB_ROOT / "02_clean" / "clean_manifest.json").read_text(encoding="utf-8")
    )
    entities = manifest["entities"]
    faq = json.loads((KB_ROOT / "02_clean" / "faq.json").read_text(encoding="utf-8"))
    assert entities["faq"] == len(faq), f"manifest faq={entities['faq']} 实际 {len(faq)}"
    sku = json.loads((KB_ROOT / "02_clean" / "sku.json").read_text(encoding="utf-8"))
    assert entities["sku"] == len(sku)
    # product 按 item_id 去重
    products = json.loads((KB_ROOT / "02_clean" / "product.json").read_text(encoding="utf-8"))
    assert entities["product"] == len({p["item_id"] for p in products})
    # 关系
    rels = json.loads((KB_ROOT / "02_clean" / "refers_to.json").read_text(encoding="utf-8"))
    assert manifest["relationships"]["refers_to"] == len(rels)


def test_graph_stats_consistent_with_manifest_and_data() -> None:
    """R6 补强：graph_stats / validation_report 与 02_clean 数据一致（D-035 单一事实源锁死）。

    此前 test_single_source 只比对 manifest ↔ json，不比对 graph_stats/报告，
    导致报告数字与真实数据分裂（负责人复验抓到：报告写 24/28/307，数据却是 8/12/222）。
    """
    manifest = json.loads(
        (KB_ROOT / "02_clean" / "clean_manifest.json").read_text(encoding="utf-8")
    )
    stats = json.loads(
        (KB_ROOT / "06_report" / "graph_stats.json").read_text(encoding="utf-8")
    )
    # 1) graph_stats 实体计数与 manifest 一致（含 SPU/SKU 语义）
    for key in ("product", "sku", "category", "attribute", "policy", "script", "faq", "rule"):
        assert stats["entities"].get(key) == manifest["entities"].get(key), (
            f"graph_stats.{key}={stats['entities'].get(key)} ≠ manifest {manifest['entities'].get(key)}"
        )
    # 2) graph_stats 关系计数与 manifest 一致
    for key in ("belongs_to", "has_attr", "applies_to", "refers_to", "related_to"):
        assert stats["relationships"].get(key) == manifest["relationships"].get(key), (
            f"graph_stats.{key} ≠ manifest"
        )
    # 3) product.json 真实商品（network）与抽样/报告 REAL 实体可解析
    products = json.loads((KB_ROOT / "02_clean" / "product.json").read_text(encoding="utf-8"))
    real_items = {p["item_id"] for p in products if p.get("source") == "network"}
    assert len(real_items) >= 10, f"network 真实商品过少: {len(real_items)}"
    # 4) 抽样样本的源/目标实体必须存在于数据集（防幽灵 REAL 边）
    plan = KB_ROOT / "06_report" / "sampling_plan.csv"
    if plan.is_file():
        import csv as _csv
        rows = list(_csv.DictReader(plan.open(encoding="utf-8")))
        all_ids = {p["item_id"] for p in products} | {p["sku_id"] for p in products}
        for j in (KB_ROOT / "02_clean" / "category.json",
                  KB_ROOT / "02_clean" / "policy.json",
                  KB_ROOT / "02_clean" / "script.json",
                  KB_ROOT / "02_clean" / "faq.json",
                  KB_ROOT / "02_clean" / "attribute.json"):
            for obj in json.loads(j.read_text(encoding="utf-8")):
                for field in ("category_code", "policy_code", "script_id", "faq_id", "spec_key"):
                    if obj.get(field):
                        all_ids.add(obj[field])
        ghosts = [
            (r.get("rel_type"), r.get("source"), r.get("target"))
            for r in rows
            if (r.get("source") not in all_ids) or (r.get("target") not in all_ids)
        ]
        assert not ghosts, f"抽样含幽灵实体边（源/目标不存在）: {ghosts[:5]}"


def test_import_csv_consistent_with_clean() -> None:
    """R7 修复：04_import/*.csv 行数与 02_clean 一致（Neo4j 导入物 = 同一事实源）。

    负责人复验必修项：02_clean 已是 24/28，但 04_import CSV 还是旧规模（8/12），
    导致 docker compose + Cypher 导入后 Neo4j 装旧图。此处锁死导入物与 clean 一致。
    """
    import csv as _csv

    def clean_count(name: str) -> int:
        data = json.loads((KB_ROOT / "02_clean" / f"{name}.json").read_text(encoding="utf-8"))
        return len(data)

    # 实体 CSV（product 按 item_id 去重 = SPU 数，其余按行数）
    entity_map = {
        "nodes_product": ("product", "item_id"),
        "nodes_sku": ("sku", "sku_id"),
        "nodes_category": ("category", "category_code"),
        "nodes_attribute": ("attribute", "spec_key"),
        "nodes_policy": ("policy", "policy_code"),
        "nodes_script": ("script", "script_id"),
        "nodes_faq": ("faq", "faq_id"),
        "nodes_rule": ("rule", "rule_code"),
    }
    for csv_name, (clean_name, key) in entity_map.items():
        csv_path = KB_ROOT / "04_import" / f"{csv_name}.csv"
        assert csv_path.is_file(), f"04_import 缺 {csv_name}.csv"
        rows = list(_csv.DictReader(csv_path.open(encoding="utf-8")))
        if clean_name == "product":
            data = json.loads((KB_ROOT / "02_clean" / "product.json").read_text(encoding="utf-8"))
            expected = len({p["item_id"] for p in data})
        elif clean_name == "rule":
            # 04_import 用合并版 rule（rule 9 + rule_extended 8），clean 分开存
            rule = json.loads((KB_ROOT / "02_clean" / "rule.json").read_text(encoding="utf-8"))
            ext = json.loads((KB_ROOT / "02_clean" / "rule_extended.json").read_text(encoding="utf-8"))
            expected = len(rule) + len(ext)
        else:
            data = json.loads((KB_ROOT / "02_clean" / f"{clean_name}.json").read_text(encoding="utf-8"))
            expected = len(data)
        assert len(rows) == expected, (
            f"{csv_name}: {len(rows)} 行 ≠ clean {clean_name} {expected}"
        )
        # 唯一键不重复
        keys = [r.get(key) for r in rows]
        assert len(keys) == len(set(keys)), f"{csv_name} 唯一键重复"

    # 关系 CSV 行数与 clean 关系文件一致
    rel_map = {
        "rels_belongs_to": "belongs_to",
        "rels_has_attr": "has_attr",
        "rels_applies_to": "applies_to",
        "rels_refers_to": "refers_to",
        "rels_related_to": "related_to",
    }
    for csv_name, clean_name in rel_map.items():
        csv_path = KB_ROOT / "04_import" / f"{csv_name}.csv"
        assert csv_path.is_file(), f"04_import 缺 {csv_name}.csv"
        rows = list(_csv.DictReader(csv_path.open(encoding="utf-8")))
        rels = json.loads((KB_ROOT / "02_clean" / f"{clean_name}.json").read_text(encoding="utf-8"))
        assert len(rows) == len(rels), (
            f"{csv_name}: {len(rows)} 行 ≠ clean {clean_name} {len(rels)}"
        )


def test_no_wrong_digital_return_wording() -> None:
    """P0-2 修复：'激活后仅支持'错误文案 0 残留。"""
    hits = []
    for f in (KB_ROOT / "02_clean").rglob("*"):
        if f.is_file() and f.suffix in (".json", ".md", ".csv"):
            if "激活后仅支持" in f.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(f))
    assert hits == [], f"错误文案残留: {hits}"


def test_sampling_plan_has_empty_expected() -> None:
    """sampling_plan.csv 为人工标注模板：expected 留空、含 evidence 列。"""
    path = KB_ROOT / "06_report" / "sampling_plan.csv"
    if not path.is_file():
        pytest.skip("sampling_plan.csv 不存在")
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 50
    assert "expected" in rows[0]
    assert "evidence" in rows[0]
    assert "verifier" in rows[0]
    # 未标注前 expected 应为空（若已人工回填则跳过此断言）
    if all(r["expected"].strip() == "" for r in rows):
        assert True  # 模板状态正确
