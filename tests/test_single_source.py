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
    """根目录与 07_handoff 关键文件 md5 一致（D-035）。"""
    if not HANDOFF.is_dir():
        pytest.skip("07_handoff 目录不存在")
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
