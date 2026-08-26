"""M9-R WP3 校验测试：类型事实 + alternatives + 越权输出拒绝。

对齐验收标准：条目 3（存量标题/主图默认 keep/observe）、条目 4（缺成本/缺竞品降级）、
条目 7（建议输出契约可被 M10-R 消费）。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecommerce_agent.product_lifecycle.interface import M10_CONTRACT_VERSION, to_m10_contract
from ecommerce_agent.product_lifecycle.schemas import (
    Recommendation,
    RecommendationState,
    RecommendationType,
    TargetObject,
)
from ecommerce_agent.product_lifecycle.validation import (
    validate_full_recommendation,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _rec(
    rtype: RecommendationType = RecommendationType.PRICING,
    facts: dict | None = None,
    alternatives: list | None = None,
    degraded: bool = False,
) -> Recommendation:
    return Recommendation(
        recommendation_id="r1",
        type=rtype,
        target=TargetObject(store_id="s1", item_id="i1", sku_id="sku1"),
        facts_snapshot=facts or {},
        rationale="test",
        alternatives=(alternatives if alternatives is not None
                      else [RecommendationType.EXPERIMENT]),
        degraded=degraded,
        created_at=NOW,
        updated_at=NOW,
    )


def test_pricing_requires_cost_or_degraded() -> None:
    """定价建议缺成本 → 必须 degraded（对齐条目 4：缺成本不出正式利润安全价格）。"""
    # 缺 cost_ready 且未 degraded → 抛
    with pytest.raises(ValueError, match="missing_required_facts"):
        validate_full_recommendation(_rec())
    # degraded 建议可带缺失事实，但必须列出缺什么（missing_evidence 非空）
    rec = _rec(facts={}, degraded=True)
    rec = rec.model_copy(update={"missing_evidence": ["cost_ready"]})
    validate_full_recommendation(rec)  # 不抛（degraded + 明确缺什么）
    # cost_ready=None 视为缺失（非 degraded → 抛）
    rec_none = _rec(facts={"cost_ready": None})
    with pytest.raises(ValueError, match="missing_required_facts"):
        validate_full_recommendation(rec_none)
    # degraded 但 missing_evidence 为空 → 抛（degraded_requires_missing_evidence）
    rec_degraded_empty = _rec(facts={}, degraded=True)
    with pytest.raises(ValueError, match="degraded_requires_missing_evidence"):
        validate_full_recommendation(rec_degraded_empty)


def test_alternatives_required() -> None:
    """建议必须带 alternatives（B3 备选路径）。"""
    rec = _rec(facts={"cost_ready": True}, alternatives=[])
    with pytest.raises(ValueError, match="requires_alternatives"):
        validate_full_recommendation(rec)

def test_model_output_forbidden_key_rejected() -> None:
    """建议内容含 effect/平台权重 → 整体拒绝（含嵌套/自然语言）。"""
    rec = _rec(facts={"cost_ready": True, "effect": 0.5})
    with pytest.raises(ValueError, match="forbidden_output_key"):
        validate_full_recommendation(rec)
    # 嵌套键
    rec = _rec(facts={"cost_ready": True, "details": {"effect": 0.5}})
    with pytest.raises(ValueError, match="forbidden_output_key"):
        validate_full_recommendation(rec)
    # 自然语言越权
    rec = _rec(facts={"cost_ready": True}).model_copy(
        update={"rationale": "平台权重提升20%"}
    )
    with pytest.raises(ValueError, match="forbidden_output_key"):
        validate_full_recommendation(rec)


def test_m10_contract_wraps() -> None:
    """建议输出可包装成 M10-R 契约（对齐条目 7）。"""
    rec = _rec(facts={"cost_ready": True})
    contract = to_m10_contract(rec)  # type: ignore[arg-type]  # Recommendation 有 model_dump
    assert contract["contract_version"] == M10_CONTRACT_VERSION
    assert contract["payload"]["recommendation_id"] == "r1"


def test_stock_item_keep_observe_default() -> None:
    """存量标题/主图默认 keep/observe（对齐条目 3：无证据不改）。"""
    # 默认状态是 DRAFT（keep/observe 语义），不产生「建议改标题」
    rec = _rec(facts={}, degraded=True)
    assert rec.state is RecommendationState.DRAFT
    # B1：系统没有任何「改标题/换主图」建议类型（类型注册表无此项）
    from ecommerce_agent.product_lifecycle.schemas import RecommendationType
    all_types = {t.value for t in RecommendationType}
    assert "改标题" not in all_types
    assert "换主图" not in all_types


def test_price_conclusion_without_cost_ready_rejected() -> None:
    """B2（盲点 #4 修复）：缺成本时 rationale 含价格动作结论 → 确定性拒绝。

    任务书 WP3 L364"缺成本时不能输出正式利润安全价格"。修复前只有 prompt 软约束，
    模型产出"建议提价 2 元"可过校验落库。修复后 missing_evidence 含 cost_ready 且
    rationale 含提价/降价等动作词 → ValueError（确定性硬校验）。
    """
    from datetime import UTC, datetime

    import pytest

    from ecommerce_agent.product_lifecycle.engine import RecommendationEngine
    from ecommerce_agent.product_lifecycle.schemas import (
        RecommendationType,
    )
    from ecommerce_agent.product_diagnosis.diagnosis import (
        Diagnosis,
        DiagnosisType,
    )
    from ecommerce_agent.product_read_model.models import (
        AggregateRule,
        Granularity,
        MetricValue,
        SKUReadModel,
    )

    _missing = MetricValue.missing(
        Granularity.DAILY, AggregateRule.SUM, "2026-08-17", "test"
    )
    sku = SKUReadModel(
        tenant_id="t1", store_id="store-1", item_id="item-1", sku_id="sku-1",
        revision=1, impressions=_missing, clicks=_missing, add_to_cart=_missing,
        orders=_missing, payments=_missing, refunds=_missing, net_sales=_missing,
        sellable_stock=_missing, in_transit_stock=_missing,
    )
    diag = Diagnosis(
        diagnosis_type=DiagnosisType.AD_PRICE_POLLUTION,
        sku_id="sku-1",
        reason="pollution:price_change",
        evidence_facts={
            "evidence_state": "actual", "freshness": {"usable_as_current": True},
            "quality_gate": "passed", "quality_gate_issues": [],
            "exposures": 1000.0, "clicks": 100.0, "conversions": 10.0,
            "stockout": False, "pollution": "price_change",
        },
        degraded=True,
    )

    class _PriceInterpreter:
        """mock 建议解释器：产出 PRICING 类型 + 价格动作结论 rationale。"""

        def interpret(self, diagnosis, decision_facts=None):
            from ecommerce_agent.product_lifecycle.engine import (
                RecommendationCandidate,
            )

            return RecommendationCandidate(
                type=RecommendationType.PRICING,
                rationale="竞争价格偏低，建议提价 2 元",
            )

    engine = RecommendationEngine(interpreter=_PriceInterpreter())
    # 缺成本（PRICING facts_snapshot 恒空 → missing_evidence 含 cost_ready）
    # + rationale 含"提价" → 确定性拒绝
    with pytest.raises(ValueError, match="price_conclusion_without_cost_ready"):
        engine.generate(
            tenant_id="t1", diagnosis=diag, sku=sku,
            recommendation_id="rec-price", created_at=datetime(2026, 8, 20, tzinfo=UTC),
        )
