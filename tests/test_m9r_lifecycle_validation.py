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
    validate_model_output,
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
    """模型输出含 effect/平台权重 → 整体拒绝（含嵌套/自然语言）。"""
    rec = _rec(facts={"cost_ready": True})
    with pytest.raises(ValueError, match="forbidden_output_key"):
        validate_model_output(rec, {"effect": 0.5})
    # 嵌套键
    with pytest.raises(ValueError, match="forbidden_output_key"):
        validate_model_output(rec, {"details": {"effect": 0.5}})
    # 自然语言越权
    with pytest.raises(ValueError, match="forbidden_output_key"):
        validate_model_output(rec, {"notes": ["平台权重提升20%"]})


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
