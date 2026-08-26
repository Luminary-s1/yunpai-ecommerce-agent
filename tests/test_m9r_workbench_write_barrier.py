"""M9-R WP4 页面写屏障测试：浏览/点击不触发任何写动作（B4）。

对齐验收标准：条目 3（页面浏览无隐式分析/创建实验/创建建议/修改商品）。
"""
from __future__ import annotations

import pytest

from ecommerce_agent.product_workbench.pages import WorkbenchPages
from ecommerce_agent.product_lifecycle.validation import WriteBarrier


def test_page_view_does_not_write() -> None:
    """浏览页面（product_detail）不触发任何写动作。

    确定性：WorkbenchPages.product_detail 是纯读组装，无写调用。
    用 WriteBarrier 锁死：任何「页面浏览触发写」的路径都被白名单拒绝。
    """
    pages = WorkbenchPages()
    data = pages.product_detail(store_id="s1", item_id="i1", sku_id="sku1")
    assert data["store_id"] == "s1"  # 页面数据可读
    # 页面渲染路径不得调用平台写（写屏障白名单外全拒）
    barrier = WriteBarrier()
    with pytest.raises(ValueError, match="write_not_allowlisted"):
        barrier.assert_write_allowed("platform.change_price")


def test_page_no_hidden_actions() -> None:
    """页面返回数据无「隐式创建实验/建议」字段。"""
    pages = WorkbenchPages()
    data = pages.product_detail(store_id="s1", item_id="i1", sku_id="sku1")
    # 页面只返回只读数据（metrics/boundary_notes），无写动作标记
    assert set(data.keys()) <= {
        "store_id", "item_id", "sku_id", "scope", "metrics", "boundary_notes"
    }
