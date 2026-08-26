"""M9-R WP3 写屏障测试：批准不触发平台动作（B2/B4）。

对齐验收标准：条目 2（批准不触发平台写动作）。
"""
from __future__ import annotations

import pytest

from ecommerce_agent.product_lifecycle.validation import WriteBarrier


def test_allowlisted_internal_writes_pass() -> None:
    """内部写白名单（建议/状态机/审计）允许。"""
    barrier = WriteBarrier()
    for action in ("recommendation.create", "recommendation.state_transition",
                   "recommendation.audit"):
        barrier.assert_write_allowed(action)  # 不抛


def test_platform_write_rejected() -> None:
    """平台写（改价/换图/报名/调广告）→ 拒绝。"""
    barrier = WriteBarrier()
    for action in ("platform.change_price", "platform.update_image",
                   "platform.enroll_promotion", "platform.adjust_ad"):
        with pytest.raises(ValueError, match="write_not_allowlisted"):
            barrier.assert_write_allowed(action)
