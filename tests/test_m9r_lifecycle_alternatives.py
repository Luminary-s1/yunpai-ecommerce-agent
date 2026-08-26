"""M9-R WP3 备选路径测试：建议必须有 alternatives（B3）。

对齐验收标准：条目 6（每条建议带备选路径）。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecommerce_agent.product_lifecycle.schemas import (
    Recommendation,
    RecommendationType,
    TargetObject,
)
from ecommerce_agent.product_lifecycle.validation import validate_full_recommendation

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _rec(alternatives: list[RecommendationType]) -> Recommendation:
    return Recommendation(
        recommendation_id="r1",
        type=RecommendationType.DIAGNOSIS,
        target=TargetObject(store_id="s1", item_id="i1", sku_id="sku1"),
        facts_snapshot={"traffic_facts": {"exposures": 100}},
        rationale="test",
        alternatives=alternatives,
        created_at=NOW,
        updated_at=NOW,
    )


def test_alternatives_must_include_launch_or_experiment() -> None:
    """备选路径必须含上新或受控实验（任务书硬边界）。"""
    rec = _rec([RecommendationType.EXPERIMENT])
    validate_full_recommendation(rec)  # 含受控实验 → 通过


def test_alternatives_empty_rejected() -> None:
    """无 alternatives → 抛。"""
    rec = _rec([])
    with pytest.raises(ValueError, match="requires_alternatives"):
        validate_full_recommendation(rec)


def test_alternatives_irrelevant_rejected() -> None:
    """备选路径不含上新/实验 → 抛（B3 强制备选路径）。"""
    rec = _rec([RecommendationType.CLEARANCE])
    with pytest.raises(ValueError, match="include_launch_or_experiment"):
        validate_full_recommendation(rec)
