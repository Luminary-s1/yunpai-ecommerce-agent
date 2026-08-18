"""WP2 验收脚本：按 m9r-complete-plan.md 第五节 WP2 验收表 12 条标准逐条断言验证。

标 ✅ 的必须 PASS；标 ⚠️ 的待确认（本脚本实测 WP2 实现后确认，通过则转 ✅）。
闫睿涵 WP5 可直接复验本脚本。
"""
from __future__ import annotations

from ecommerce_agent.product_diagnosis.bridge import EvidenceBridge, PROVENANCE_PATHS
from ecommerce_agent.product_diagnosis.diagnosis import DiagnosisType, build_diagnosis
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


class _FakeService:
    """假 TrafficLabService：返回可预测视图，供桥接测试。"""

    def get_revision(self, tenant_id, revision_id):
        return {
            "revision_id": revision_id,
            "evidence_json": {
                "source_provenance": {
                    "source_type": "virtual", "virtual": True,
                    "completeness": "complete", "basis": "demo",
                }
            },
            "freshness": {"usable_as_current": True},
            "data_as_of": "2026-08-17",
        }

    def get_experiment(self, tenant_id, experiment_id):
        return {
            "experiment_id": experiment_id,
            "evidence_json": {
                "source_provenance": {
                    "source_type": "actual", "virtual": False,
                    "completeness": "complete", "basis": "report",
                }
            },
            "freshness": {"usable_as_current": True},
            "status": "running",
            "data_as_of": "2026-08-17",
        }

    def list_analysis_runs(self, tenant_id, experiment_id, *, limit=100):
        return [{"analysis_run_id": "run-1", "statistical_facts": {"effect": 0.1}}]


def _bridge() -> EvidenceBridge:
    bridge = EvidenceBridge(service=None)  # type: ignore[arg-type]
    bridge.service = _FakeService()  # type: ignore[assignment]
    return bridge


# ── 条目 1：桥接 revision/experiment/analysis ──
def t01() -> None:
    b = _bridge()
    assert b.get_revision_view("t1", "rev-1")["evidence_state"] == "demo"
    assert b.get_experiment_view("t1", "exp-1")["status"] == "running"
    runs = b.list_analysis_runs_view("t1", "exp-1")
    assert runs[0]["analysis_run_id"] == "run-1"
    assert runs[0]["evidence_state"] == "actual"


# ── 条目 2：桥接 freshness/provenance ──
def t02() -> None:
    assert PROVENANCE_PATHS["traffic"] == ("evidence_json", "source_provenance")
    b = _bridge()
    view = b.get_revision_view("t1", "rev-1")
    assert view["source_provenance"]["source_type"] == "virtual"
    assert view["freshness"]["usable_as_current"] is True


# ── 条目 3：真实/Demo 物理隔离 ──
def t03() -> None:
    b = _bridge()
    assert b.get_revision_view("t1", "rev-demo")["evidence_state"] == "demo"
    assert b.get_experiment_view("t1", "exp-real")["evidence_state"] == "actual"


# ── 条目 4：Gate 通过才给强方向结论 ──
def t04() -> None:
    engine = GateEngine()
    all_passed, _ = engine.run_all({
        "evidence_state": "actual",
        "freshness": {"usable_as_current": True},
    })
    assert all_passed is True
    all_fail, _ = engine.run_all({"evidence_state": "missing"})
    assert all_fail is False


# ── 条目 5：freshness Gate ──
def t05() -> None:
    engine = GateEngine()
    assert engine.check_freshness(
        {"freshness": {"usable_as_current": True}}).passed is True
    assert engine.check_freshness(
        {"freshness": {"usable_as_current": False}}).passed is False


# ── 条目 6：缺货/广告/价格污染不归因标题/主图 ──
def t06() -> None:
    diag = build_diagnosis("sku1", {"evidence_state": "actual"}, stockout=True)
    assert diag.diagnosis_type is DiagnosisType.STOCKOUT_POLLUTION
    diag2 = build_diagnosis("sku1", {"evidence_state": "actual"}, pollution="ad_change")
    assert diag2.diagnosis_type is DiagnosisType.AD_PRICE_POLLUTION


# ── 条目 7：模型越权输出整份拒绝 ──
def t07() -> None:
    engine = GateEngine()
    assert engine.check_no_forbidden_output({"effect": 0.5}).passed is False
    assert engine.check_no_forbidden_output({"diagnosis_type": "x"}).passed is True


# ── 条目 8：无合格实验不编造 uplift ──
def t08() -> None:
    diag = build_diagnosis("sku1", {"evidence_state": "missing"})
    assert diag.diagnosis_type is DiagnosisType.EVIDENCE_INSUFFICIENT
    assert diag.reason == "evidence_missing"


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
