"""M9-R WP4 机制 Eval 测试：冻结场景 oracle 断言。

对齐验收标准：条目 4（Eval 发现真实方向 + 拒绝污染方向）、
条目 3（页面浏览无隐式写动作）。
"""
from __future__ import annotations

from ecommerce_agent.product_workbench.eval import (
    EvalResult,
    MechanismEvalRunner,
    _EVAL_CREATED_AT,
)
from ecommerce_agent.product_workbench.scenes import FROZEN_SCENES, FrozenScene


def test_frozen_scenes_non_empty() -> None:
    """至少 7 个冻结场景（覆盖任务书七类方向）。"""
    assert len(FROZEN_SCENES) >= 7


def test_eval_detects_stockout_pollution() -> None:
    """缺货污染场景：诊断必须标记 STOCKOUT_POLLUTION + degraded。"""
    runner = MechanismEvalRunner()
    results = runner.run_all()
    stockout = next(r for r in results if r.scene_name == "缺货污染")
    assert stockout.passed is True, stockout.failures


def test_eval_rejects_pollution_for_clean_data() -> None:
    """合格数据场景：无污染/无不足（不编造问题）。"""
    runner = MechanismEvalRunner()
    results = runner.run_all()
    clean = next(r for r in results if r.scene_name == "存量保持")
    assert clean.passed is True, clean.failures


def test_eval_rejects_pollution_marker_without_pollution() -> None:
    """反证：解释器对无污染的干净数据给 STOCKOUT_POLLUTION → 校验拒绝。"""
    from ecommerce_agent.product_diagnosis.diagnosis import (
        build_diagnosis_facts,
        validate_diagnosis_output,
    )

    facts = build_diagnosis_facts(
        "sku-x",
        {
            "evidence_state": "actual",
            "exposures": 5000,
            "clicks": 400,
            "quality_gate": {"status": "passed", "issues": []},
        },
    )
    try:
        validate_diagnosis_output(
            facts,
            {"diagnosis_type": "stockout_pollution", "reason": "fake"},
        )
        assert False, "should reject pollution marker without pollution"
    except ValueError:
        pass


def test_eval_rejects_stockout_as_ad_price_pollution() -> None:
    """负责人复验阻断项 5：缺货证据不得被解释成广告/价格污染。

    修复前污染校验只查 `stockout OR pollution` 任一存在——facts 只有缺货（stockout）
    时 `ad_price_pollution` 仍被接受，下游固定映射会导向"定价候选"而非"补货联动"
    （错误的安全方向）。修复后污染子类型分别锁定。
    """
    from ecommerce_agent.product_diagnosis.diagnosis import (
        build_diagnosis_facts,
        validate_diagnosis_output,
    )

    facts = build_diagnosis_facts(
        "sku-x",
        {
            "evidence_state": "actual",
            "exposures": 1000,
            "clicks": 100,
            "quality_gate": {"status": "passed", "issues": []},
        },
        stockout=True,  # 只有缺货证据，无广告/价格变化
    )
    try:
        validate_diagnosis_output(
            facts,
            {"diagnosis_type": "ad_price_pollution", "reason": "fake"},
        )
        assert False, "缺货证据不应通过 ad_price_pollution 校验"
    except ValueError:
        pass
    # 反向：缺货证据 → stockout_pollution 正常通过
    diag = validate_diagnosis_output(
        facts,
        {"diagnosis_type": "stockout_pollution", "reason": "stockout_observed"},
    )
    assert diag.diagnosis_type.value == "stockout_pollution"


def test_eval_summary_all_pass() -> None:
    """全部冻结场景通过 oracle。"""
    runner = MechanismEvalRunner()
    passed, total = runner.summary()
    assert passed == total
    assert total >= 7


def test_eval_covers_all_seven_directions() -> None:
    """冻结场景覆盖任务书七类方向。"""
    names = {scene.name for scene in FROZEN_SCENES}
    assert {
        "选品方向", "上新准备", "存量保持", "受控优化",
        "缺货污染", "缺数据", "清仓风险",
    } <= names


def test_eval_result_type() -> None:
    runner = MechanismEvalRunner()
    results = runner.run_all()
    assert all(isinstance(r, EvalResult) for r in results)


def test_eval_produces_recommendation_type() -> None:
    """机制 Eval 产出建议类型（诊断 → 建议生产链路，非只测 degraded）。

    反假绿：每个 PASS 场景的 recommendation_type 必须与冻结场景 expected
    一致；若删除 eval.py 的 recommendation 层（或解释器产出错误建议类型），
    本测试必须 FAIL。
    """
    from ecommerce_agent.product_workbench.scenes import FROZEN_SCENES

    runner = MechanismEvalRunner()
    results = runner.run_all()
    # 场景名 → expected.recommendation_type
    expected_by_name = {
        scene.name: scene.expected.get("recommendation_type")
        for scene in FROZEN_SCENES
    }
    # 每个场景必须有推荐类型预期
    assert all(v is not None for v in expected_by_name.values()), (
        "冻结场景缺 recommendation_type 预期"
    )
    # 重跑每个场景，断言 produced 的 recommendation_type 与预期一致
    for scene in FROZEN_SCENES:
        r = runner.run_scene(scene)
        assert r.passed, f"{scene.name} 未通过: {r.failures}"
        # 直接重放 produced（scene.run_oracle 已校验 diagnosis_type + recommendation_type）
        # 双保险：显式断言 recommendation_type 匹配预期
        produced = _replay_produced(runner, scene)
        assert produced["recommendation_type"] == expected_by_name[scene.name], (
            f"{scene.name} 建议类型 {produced['recommendation_type']} != "
            f"预期 {expected_by_name[scene.name]}"
        )


def _replay_produced(runner, scene) -> dict:
    """重放单场景的 produced（诊断 + 建议全链），用于显式断言。"""
    from ecommerce_agent.product_diagnosis.diagnosis import build_diagnosis_facts
    from ecommerce_agent.product_diagnosis.interpreter import run_interpretation

    d = scene.input_data
    facts = build_diagnosis_facts(
        d["sku_id"],
        {
            "evidence_state": d.get("evidence_state"),
            "exposures": d.get("exposures"),
            "clicks": d.get("clicks"),
            "conversions": d.get("conversions"),
            "quality_gate": d.get("quality_gate"),
        },
        stockout=d.get("stockout", False),
        pollution=d.get("pollution"),
    )
    diag = run_interpretation(facts, runner.interpreter)
    from ecommerce_agent.product_read_model.models import (
        AggregateRule,
        Granularity,
        MetricValue,
        SKUReadModel,
    )
    from ecommerce_agent.readonly_data.contracts import EvidenceState

    _missing = MetricValue.missing(
        Granularity.DAILY, AggregateRule.SUM, "2026-08-17", "eval"
    )
    sku = SKUReadModel(
        tenant_id="t1",
        store_id="store-eval",
        item_id="item-eval",
        sku_id=d["sku_id"],
        revision=1,
        impressions=_missing,
        clicks=_missing,
        add_to_cart=_missing,
        orders=_missing,
        payments=_missing,
        refunds=_missing,
        net_sales=_missing,
        sellable_stock=_missing,
        in_transit_stock=_missing,
    )
    rec = runner.recommendation_engine.generate(
        tenant_id="t1",
        diagnosis=diag,
        sku=sku,
        recommendation_id="eval-rec",
        created_at=_EVAL_CREATED_AT,
    )
    return {
        "diagnosis_type": diag.diagnosis_type.value,
        "degraded": diag.degraded,
        "recommendation_type": rec.type.value,
    }


def test_eval_mutation_wrong_direction_fails() -> None:
    """P1-4 反证 mutation：解释器对缺货返回错误方向 → eval 必须失败。

    复验指出「让解释器始终返回错误的 EVIDENCE_INSUFFICIENT，9 个场景仍有
    7 个 PASS」——oracle 只锁 degraded 不锁方向导致自洽假绿。修复后
    oracle 锁 diagnosis_type + recommendation_type，错误方向必须 FAIL。
    """
    from ecommerce_agent.product_diagnosis.diagnosis import DiagnosisType
    from ecommerce_agent.product_diagnosis.interpreter import DiagnosisInterpreter

    class _WrongInterpreter(DiagnosisInterpreter):
        """故意错误：缺货污染场景返回 EVIDENCE_INSUFFICIENT（错误方向）。"""

        def interpret(self, facts):
            from ecommerce_agent.product_diagnosis.diagnosis import DiagnosisFacts
            from typing import Any, Mapping

            if facts.stockout:
                return {"diagnosis_type": "evidence_insufficient", "reason": "mutation"}
            if facts.evidence_state in (None, "missing"):
                return {"diagnosis_type": "evidence_insufficient", "reason": "evidence_missing"}
            return {"diagnosis_type": "evidence_insufficient", "reason": "mutation"}

    runner = MechanismEvalRunner(interpreter=_WrongInterpreter())
    results = runner.run_all()
    stockout = next(r for r in results if r.scene_name == "缺货污染")
    # 缺货污染：错误方向必须 FAIL（不 PASS）
    assert stockout.passed is False, (
        f"mutation 后缺货污染仍 PASS（oracle 未锁方向，自洽假绿）：{stockout.failures}"
    )


def test_eval_mutation_always_evidence_insufficient_fails() -> None:
    """P1-4 反证 mutation：解释器对所有场景返回 EVIDENCE_INSUFFICIENT → 必失败。"""
    from ecommerce_agent.product_diagnosis.interpreter import DiagnosisInterpreter

    class _AlwaysInsufficient(DiagnosisInterpreter):
        def interpret(self, facts):
            return {"diagnosis_type": "evidence_insufficient", "reason": "always"}

    runner = MechanismEvalRunner(interpreter=_AlwaysInsufficient())
    results = runner.run_all()
    # 广告/价格污染场景：错误方向必须 FAIL（oracle 锁 AD_PRICE_POLLUTION）
    pollution = next(r for r in results if r.scene_name == "广告/价格污染")
    assert pollution.passed is False, (
        f"mutation 后广告/价格污染仍 PASS：{pollution.failures}"
    )
    # 缺货污染场景：错误方向必须 FAIL（oracle 锁 STOCKOUT_POLLUTION）
    stockout = next(r for r in results if r.scene_name == "缺货污染")
    assert stockout.passed is False, (
        f"mutation 后缺货污染仍 PASS：{stockout.failures}"
    )
    # P4 目标（第三波）：选品/上新/存量/受控/缺数据/清仓/生命周期等"期望
    # EVIDENCE_INSUFFICIENT+保持观察"的场景，若引擎扩展后 oracle 锁真实方向，
    # mutation 应使 ≥8/10 场景失败。当前阶段（引擎未扩展）污染类场景已锁方向。
    failed = [r.scene_name for r in results if not r.passed]
    assert len(failed) >= 2, (
        f"mutation 后失败场景应至少含污染类: {failed}"
    )


def test_eval_direction_scenes_reachable_non_degraded() -> None:
    """R5（负责人阻断项 5 修复）：方向场景注入信号 + mock 建议解释器 → 非降级真实方向。

    选品/上新/清仓场景不再锁"保持观察"（假覆盖），而是：mock 建议解释器产出
    SELECTION/NEW_LAUNCH/CLEARANCE + REQUIRED_FACTS 信号注入 → 引擎产出非降级
    真实方向（recommendation_degraded=False + missing_evidence=[]），证明"发现
    真实方向"（任务书 L476），而非只证"降级能力"。
    """
    from ecommerce_agent.product_lifecycle.engine import (
        RecommendationCandidate,
        RecommendationInterpreter,
    )
    from ecommerce_agent.product_lifecycle.schemas import RecommendationType
    from ecommerce_agent.product_workbench.scenes import DIRECTION_SCENES

    # 方向建议解释器：基于 evidence_facts 信号键值决定方向（与生产模型一致——
    # 输入事实决定语义方向，SKU 身份无关）。盲测：即使把场景 SKU 改成任意盲名，
    # 只要信号键值不变，方向就不变（ground truth 不通过身份标签进入输入，L476）。
    _SIGNAL_BY_TYPE = (
        (RecommendationType.EXPERIMENT, ("revision_evidence",)),
        (RecommendationType.PROMOTION, ("campaign_window",)),
        (RecommendationType.SELECTION, ("demand_signal", "competitor_evidence")),
        (RecommendationType.NEW_LAUNCH, ("item_ready", "stock_ready")),
        (RecommendationType.CLEARANCE, ("clearance_signal", "competitor_evidence")),
    )

    class _DirectionInterpreter(RecommendationInterpreter):
        def interpret(self, diagnosis):
            facts = diagnosis.evidence_facts
            for recommendation_type, required_signals in _SIGNAL_BY_TYPE:
                if all(facts.get(key) for key in required_signals):
                    return RecommendationCandidate(
                        type=recommendation_type,
                        rationale="方向信号齐备，建议候选生成（mock 语义层）",
                    )
            return RecommendationCandidate(
                type=RecommendationType.KEEP_OBSERVE,
                rationale="信号不足，保持观察（mock 语义层）",
            )

    runner = MechanismEvalRunner(
        scenes=DIRECTION_SCENES,
        recommendation_interpreter=_DirectionInterpreter(),
    )
    results = runner.run_all()
    assert len(results) == 5, f"方向场景应为 5 个: {len(results)}"
    for r in results:
        assert r.passed, f"{r.scene_name} 未通过: {r.failures}"
        assert r.scene_name in (
            "选品方向", "上新准备", "清仓风险", "受控优化", "活动候选",
        )
    # 盲测：把方向场景 SKU 全部重命名为随机盲名后，方向仍由信号决定，全部 PASS。
    # 若 Eval 依赖 SKU 名标签（答案编码），重命名会让对应场景变红（负责人阻断项 6）。
    renamed = [
        FrozenScene(
            s.name,
            input_data={**s.input_data, "sku_id": f"renamed-{s.name}"},
            expected=s.expected,
        )
        for s in DIRECTION_SCENES
    ]
    blind_runner = MechanismEvalRunner(
        scenes=renamed,
        recommendation_interpreter=_DirectionInterpreter(),
    )
    blind_results = blind_runner.run_all()
    assert all(r.passed for r in blind_results), (
        f"SKU 重命名后方向不应改变（答案编码?）: "
        f"{[r.failures for r in blind_results if not r.passed]}"
    )


def test_eval_direction_scenes_mutation_wrong_direction_fails() -> None:
    """R5 mutation 反证：mock 建议解释器返回 KEEP_OBSERVE → 方向场景必须 FAIL。

    若 oracle 只锁 degraded 不锁方向（自洽假绿），错误方向仍会 PASS——本测试证明
    方向场景的 oracle 锁真实 recommendation_type（非"保持观察"）。
    """
    from ecommerce_agent.product_lifecycle.engine import (
        RecommendationCandidate,
        RecommendationInterpreter,
    )
    from ecommerce_agent.product_lifecycle.schemas import RecommendationType
    from ecommerce_agent.product_workbench.scenes import DIRECTION_SCENES

    class _WrongDirection(RecommendationInterpreter):
        def interpret(self, diagnosis):
            return RecommendationCandidate(
                type=RecommendationType.KEEP_OBSERVE,
                rationale="mutation: 错误方向保持观察",
            )

    runner = MechanismEvalRunner(
        scenes=DIRECTION_SCENES,
        recommendation_interpreter=_WrongDirection(),
    )
    results = runner.run_all()
    # 错误方向（保持观察）≠ 期望方向（选品候选等）→ 全部 FAIL
    assert all(not r.passed for r in results), (
        f"错误方向不应 PASS: {[r.failures for r in results if r.passed]}"
    )
