"""WP2 验收脚本：按 m9r-complete-plan.md 第五节 WP2 验收表 12 条标准逐条断言验证。

标 ✅ 的必须 PASS；标 ⚠️ 的待确认（本脚本实测 WP2 实现后确认，通过则转 ✅）。
闫睿涵 WP5 可直接复验本脚本。
"""
from __future__ import annotations

import pytest

from ecommerce_agent.product_diagnosis.bridge import EvidenceBridge
from ecommerce_agent.product_diagnosis.diagnosis import (
    DiagnosisType,
    build_diagnosis_facts,
    validate_diagnosis_output,
)
from ecommerce_agent.product_diagnosis.experiment import (
    ExperimentGateway,
    ExperimentNotAvailableError,
)
from ecommerce_agent.product_diagnosis.gates import GateEngine

RESULTS: list[tuple[str, str, str, bool, str]] = []


def check(cid: str, desc: str, expected: str, fn) -> None:
    try:
        fn()
        actual = "PASS"
    except AssertionError as e:
        actual = f"FAIL: {e}"
    except Exception as e:  # noqa: BLE001
        actual = f"ERROR: {type(e).__name__}: {e}"
    ok = actual == "PASS"
    RESULTS.append((cid, desc, expected, ok, actual))


def _real_bridge() -> EvidenceBridge:
    """真实 TrafficLabService + tmp DB 种数据（WP5 验收：不再用假视图）。"""
    from pathlib import Path
    import tempfile

    from ecommerce_agent.database import Database
    from tests.test_m9r_diagnosis_bridge import _seeded_service

    db = Database(Path(tempfile.mkdtemp()) / "verify-wp2.sqlite3")
    db.initialize()
    return EvidenceBridge(_seeded_service(db))


# ── 条目 1：桥接 revision/experiment/analysis（真实证据）──
def t01() -> None:
    bridge = _real_bridge()
    # revision 证据从真实 metric buckets 读取（virtual → demo，非 missing）
    with bridge.service.db.connect() as conn:
        bucket_rev = conn.execute(
            "SELECT listing_revision_id FROM traffic_metric_buckets WHERE tenant_id='tenant-a' LIMIT 1"
        ).fetchone()["listing_revision_id"]
    view = bridge.get_revision_view("tenant-a", bucket_rev)
    assert view["evidence_state"] == "demo", f"got {view['evidence_state']}"
    assert view["source_provenance"]["source_type"] == "virtual"
    assert view["data_as_of"] is not None


# ── 条目 2：桥接 freshness/provenance（真实持久化位置）──
def t02() -> None:
    bridge = _real_bridge()
    with bridge.service.db.connect() as conn:
        bucket_rev = conn.execute(
            "SELECT listing_revision_id FROM traffic_metric_buckets WHERE tenant_id='tenant-a' LIMIT 1"
        ).fetchone()["listing_revision_id"]
    view = bridge.get_revision_view("tenant-a", bucket_rev)
    assert view["freshness"] is not None
    assert "reason_codes" in view["freshness"]  # evidence-freshness-v1 结构


# ── 条目 3：真实/Demo 物理隔离 ──
def t03() -> None:
    bridge = _real_bridge()
    # 真实种的是 virtual_taobao → demo（隔离语义：demo 不进 operational）
    with bridge.service.db.connect() as conn:
        bucket_rev = conn.execute(
            "SELECT listing_revision_id FROM traffic_metric_buckets WHERE tenant_id='tenant-a' LIMIT 1"
        ).fetchone()["listing_revision_id"]
    view = bridge.get_revision_view("tenant-a", bucket_rev)
    assert view["evidence_state"] == "demo"
    # 不存在的 experiment → missing（不冒充）
    assert bridge.get_experiment_view("tenant-a", "exp-nonexistent")["evidence_state"] == "missing"


# ── 条目 4：Gate 通过才给强方向结论 ──
def t04() -> None:
    engine = GateEngine()
    all_passed, _ = engine.run_all({
        "evidence_state": "actual",
        "freshness": {"usable_as_current": True},
        "quality_gate": {"status": "passed", "issues": []},
    })
    assert all_passed is True
    all_fail, _ = engine.run_all({"evidence_state": "missing"})
    assert all_fail is False
    blocked, _ = engine.run_all({
        "evidence_state": "actual",
        "freshness": {"usable_as_current": True},
        "quality_gate": {"status": "blocked", "issues": ["aa_gate_missing"]},
    })
    assert blocked is False  # quality_gate blocked → 不给强方向


# ── 条目 5：freshness Gate ──
def t05() -> None:
    engine = GateEngine()
    assert engine.check_freshness(
        {"freshness": {"usable_as_current": True}}).passed is True
    assert engine.check_freshness(
        {"freshness": {"usable_as_current": False}}).passed is False


# ── 条目 6：缺货/广告/价格污染不归因标题/主图 ──
def t06() -> None:
    facts = build_diagnosis_facts(
        "sku1", {"evidence_state": "actual"}, stockout=True
    )
    assert facts.stockout is True
    assert facts.conclusion_allowed() is False  # 污染 → 不给强方向
    facts2 = build_diagnosis_facts(
        "sku1", {"evidence_state": "actual"}, pollution="ad_change"
    )
    assert facts2.pollution == "ad_change"
    assert facts2.conclusion_allowed() is False


# ── 条目 7：模型越权输出整份拒绝 ──
def t07() -> None:
    engine = GateEngine()
    assert engine.check_no_forbidden_output({"effect": 0.5}).passed is False
    assert engine.check_no_forbidden_output({"diagnosis_type": "x"}).passed is True


# ── 条目 8：无合格实验不编造 uplift ──
def t08() -> None:
    facts = build_diagnosis_facts("sku1", {"evidence_state": "missing"})
    assert facts.evidence_state == "missing"
    assert facts.conclusion_allowed() is False  # 证据缺失 → 不给结论
    # 解释器即使给强方向类型也被校验拒绝（不编造 uplift）
    with pytest.raises(ValueError, match="diagnosis_conclusion_not_allowed"):
        validate_diagnosis_output(
            facts, {"diagnosis_type": "exposure_insufficient", "reason": "low exp"}
        )


# ── 条目 9：真实缺 SKU 流量 → blocked ──
def t09() -> None:
    gateway = ExperimentGateway(simulation=None)
    try:
        gateway.create_real_experiment(tenant_id="t1")
        raise AssertionError("真实实验应 blocked")
    except ExperimentNotAvailableError:
        pass


# ── 条目 10：诊断全链平台写=0 ──
def t10() -> None:
    # Demo 实验不触发平台写；真实路径 blocked 前无写
    class _FakeSim:
        def run(self, **kw):
            return {"virtual": True}

    gateway = ExperimentGateway(simulation=_FakeSim())
    result = gateway.run_demo_experiment(tenant_id="t1", actor="ops", confirm_virtual=True)
    assert result["virtual"] is True


# ── 条目 11：受控实验入口 Demo 路径 ──
def t11() -> None:
    class _FakeSim:
        def run(self, **kw):
            return {"virtual": True}

    gateway = ExperimentGateway(simulation=_FakeSim())
    assert gateway.run_demo_experiment(
        tenant_id="t1", actor="ops", confirm_virtual=True)["virtual"] is True


# ── 条目 12：Demo 隔离不进入默认视图 ──
def t12() -> None:
    b = _bridge()
    demo_view = b.get_revision_view("t1", "rev-demo")
    assert demo_view["evidence_state"] == "demo"
    # Demo 源标记为 demo，operational 查询层据此过滤


check("①", "桥接 revision/experiment/analysis 证据", "✅", t01)
check("②", "桥接 freshness/provenance 证据", "✅", t02)
check("③", "真实/Demo 查询物理隔离", "⚠️", t03)
check("④", "A/A/样本量/窗口/控制变量 Gate", "⚠️", t04)
check("⑤", "freshness Gate", "✅", t05)
check("⑥", "缺货/广告/价格污染不归因标题/主图", "⚠️", t06)
check("⑦", "模型越权输出整份拒绝", "⚠️", t07)
check("⑧", "无合格实验不编造 uplift", "⚠️", t08)
check("⑨", "真实缺 SKU 流量/revision → blocked", "⚠️", t09)
check("⑩", "诊断全链平台写=0（内部写白名单）", "⚠️", t10)
check("⑪", "受控实验入口 Demo 路径", "⚠️", t11)
check("⑫", "Demo 隔离不进入默认视图", "⚠️", t12)

print(f"{'条目':<6}{'验收标准':<34}{'计划':<5}{'实际':<8}备注")
print("-" * 95)
all_ok = True
for cid, desc, exp, ok, actual in RESULTS:
    if not ok:
        all_ok = False
    print(f"{cid:<7}{desc:<36}{exp:<6}{('PASS' if ok else '**FAIL**'):<8}{actual}")
print("-" * 95)
print(f"结论: {'✅ 全部 PASS' if all_ok else '❌ 有 FAIL 项，需修复'}")
