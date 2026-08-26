"""M9-R WP4 工作台页面测试：SKU 下钻 + 四态徽标 + 试算字样 + 边界说明。

对齐验收标准：条目 1（下钻到 revision/时间窗/指标/来源/建议依据）、
条目 8（边界说明文字在页面）、条目 9（四态徽标+来源+时间）、条目 10（演示参数试算字样）。
"""
from __future__ import annotations

import pytest

from ecommerce_agent.product_workbench.boundaries import DEMO_LABEL, BOUNDARY_NOTES
from ecommerce_agent.product_workbench.pages import WorkbenchPages


def test_product_detail_drills_down() -> None:
    """SKU 下钻：返回 store/item/sku + scope + metrics。"""
    pages = WorkbenchPages()
    data = pages.product_detail(
        store_id="s1", item_id="i1", sku_id="sku1",
        metrics={"impressions": {"evidence_state": "actual",
                                 "source": "taobao",
                                 "data_as_of": "2026-08-17",
                                 "value": 100}},
    )
    assert data["store_id"] == "s1"
    assert data["item_id"] == "i1"
    assert data["sku_id"] == "sku1"
    assert data["metrics"]["impressions"]["evidence_state"] == "actual"


def test_metric_has_four_state_badge() -> None:
    """每个指标渲染四态徽标（真实数据→绿标）。"""
    pages = WorkbenchPages()
    data = pages.product_detail(
        store_id="s1", item_id="i1", sku_id="sku1",
        metrics={"impressions": {"evidence_state": "actual",
                                 "source": "taobao", "data_as_of": "2026-08-17",
                                 "value": 100}},
    )
    badge = data["metrics"]["impressions"]["badge"]
    assert badge["label"] == "真实数据"
    assert badge["color"] == "green"


def test_demo_metric_renders_trial_label() -> None:
    """演示参数必须渲染「试算」字样（条目 10）。"""
    pages = WorkbenchPages()
    data = pages.product_detail(
        store_id="s1", item_id="i1", sku_id="sku1",
        metrics={"impressions": {"evidence_state": "demo",
                                 "source": "virtual_store_v1",
                                 "data_as_of": "2026-08-17",
                                 "value": 500}},
    )
    assert data["metrics"]["impressions"]["display_label"] == DEMO_LABEL


def test_boundary_notes_in_page() -> None:
    """边界说明文字在页面（条目 8）。"""
    pages = WorkbenchPages()
    data = pages.product_detail(store_id="s1", item_id="i1", sku_id="sku1")
    assert data["boundary_notes"] == BOUNDARY_NOTES
    assert "B1" in BOUNDARY_NOTES
    assert "B4" in BOUNDARY_NOTES


def test_unknown_scope_rejected() -> None:
    """scope 未知 → 抛（不静默）。"""
    pages = WorkbenchPages()
    with pytest.raises(ValueError, match="unknown_scope"):
        pages.product_detail(store_id="s1", item_id="i1", sku_id="sku1",
                             scope="unknown")


def test_unknown_evidence_state_rejected() -> None:
    """未知 evidence_state → 抛（防漏标注，条目 9 底线）。"""
    pages = WorkbenchPages()
    with pytest.raises(ValueError, match="unknown_evidence_state_badge"):
        pages.product_detail(
            store_id="s1", item_id="i1", sku_id="sku1",
            metrics={"impressions": {"evidence_state": "bogus"}},
        )
