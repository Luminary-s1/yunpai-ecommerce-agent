"""M9-R WP3 存量标题/主图默认不改测试（B1 反例）。

对齐验收标准：条目 3（存量标题/主图默认 keep/observe）。
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ecommerce_agent.product_lifecycle.schemas import (
    Recommendation,
    RecommendationType,
    TargetObject,
)
from ecommerce_agent.product_lifecycle.validation import (
    WriteBarrier,
    validate_full_recommendation,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def test_stock_item_keeps_default_without_evidence() -> None:
    """无证据时，不得产生「建议改标题/主图」类建议。

    B1：只有「标题错误」「关键词陈旧」「曝光点击差且数据质量满足」才出人工复核建议。
    本包无标题修改建议类型 → 默认 keep/observe。
    """
    # 合法建议类型里没有「改标题」，默认保持观察（DIAGNOSIS/KEEP_OBSERVE）
    rec = Recommendation(
        recommendation_id="r1",
        type=RecommendationType.KEEP_OBSERVE,
        target=TargetObject(store_id="s1", item_id="i1", sku_id="sku1"),
        facts_snapshot={},
        rationale="no_evidence_for_change",
        alternatives=[RecommendationType.EXPERIMENT],
        degraded=True,
        created_at=NOW, updated_at=NOW,
    )
    validate_full_recommendation(rec)  # 默认 keep/observe 可通过
    # 系统没有「改标题」写动作（写屏障拒绝一切平台写）
    barrier = WriteBarrier()
    with pytest.raises(ValueError, match="write_not_allowlisted"):
        barrier.assert_write_allowed("platform.update_title")
    with pytest.raises(ValueError, match="write_not_allowlisted"):
        barrier.assert_write_allowed("platform.update_main_image")
