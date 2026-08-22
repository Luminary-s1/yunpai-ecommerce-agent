"""M9-R 越权输出递归校验测试（WP5 验收缺陷 4 反证）。

嵌套键与自然语言越权（effect/sample_size/平台权重/平台算法）必须被递归拒绝。
"""
from __future__ import annotations

import pytest

from ecommerce_agent.product_diagnosis.gates import FORBIDDEN_KEYS, GateEngine
from ecommerce_agent.product_lifecycle.validation import validate_full_recommendation
from ecommerce_agent.text_utils import contains_forbidden_token


def test_recursive_scanner_hits_nested_dict_key() -> None:
    """嵌套 dict 键命中 effect → True。"""
    assert contains_forbidden_token({"details": {"effect": 0.5}}, FORBIDDEN_KEYS)


def test_recursive_scanner_hits_list_of_strings() -> None:
    """list 内自然语言「平台权重提升20%」→ True。"""
    assert contains_forbidden_token(
        {"notes": ["平台权重提升20%"]}, FORBIDDEN_KEYS
    )


def test_recursive_scanner_hits_deep_nested_value() -> None:
    """三层嵌套值命中 sample_size → True。"""
    assert contains_forbidden_token(
        {"a": {"b": [{"c": {"sample_size": 100}}]}}, FORBIDDEN_KEYS
    )


def test_recursive_scanner_allows_clean_output() -> None:
    """无禁止键 → False（不误伤正常诊断）。"""
    assert not contains_forbidden_token(
        {"diagnosis_type": "click_insufficient", "exposures": 1000}, FORBIDDEN_KEYS
    )


def test_lifecycle_validate_rejects_nested_effect() -> None:
    """lifecycle validator 拒绝嵌套 effect（不只顶层）。"""
    from ecommerce_agent.product_lifecycle.schemas import (
        Recommendation,
        RecommendationState,
        RecommendationType,
        TargetObject,
    )
    from datetime import UTC, datetime

    rec = Recommendation(
        recommendation_id="r1",
        type=RecommendationType.KEEP_OBSERVE,
        target=TargetObject(store_id="s1"),
        facts_snapshot={"stats": {"effect": 0.5}},
        rationale="x",
        alternatives=[RecommendationType.EXPERIMENT],
        state=RecommendationState.DRAFT,
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
        updated_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="forbidden_output_key"):
        validate_full_recommendation(rec)


def test_lifecycle_validate_rejects_natural_language() -> None:
    """lifecycle validator 拒绝自然语言「平台权重」。"""
    from ecommerce_agent.product_lifecycle.schemas import (
        Recommendation,
        RecommendationState,
        RecommendationType,
        TargetObject,
    )
    from datetime import UTC, datetime

    rec = Recommendation(
        recommendation_id="r1",
        type=RecommendationType.KEEP_OBSERVE,
        target=TargetObject(store_id="s1"),
        facts_snapshot={},
        rationale="平台权重提升 20%",
        alternatives=[RecommendationType.EXPERIMENT],
        state=RecommendationState.DRAFT,
        created_at=datetime(2026, 8, 18, tzinfo=UTC),
        updated_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="forbidden_output_key"):
        validate_full_recommendation(rec)
