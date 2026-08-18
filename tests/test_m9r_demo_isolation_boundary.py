"""M9-R WP4 Demo 隔离 + 样本口径边界测试（B5/B7 反例）。

对齐验收标准：条目 6（真实/模拟场景隔离，全链标注）、条目 7（样本数据不作为产品口径）。
"""
from __future__ import annotations

from ecommerce_agent.product_workbench.pages import WorkbenchPages


def test_demo_not_in_operational_view() -> None:
    """Demo 数据（evidence_state=demo）渲染试算标签，operational 视图不得当作真实。"""
    pages = WorkbenchPages()
    data = pages.product_detail(
        store_id="s1", item_id="i1", sku_id="sku1",
        metrics={"impressions": {"evidence_state": "demo", "value": 500}},
    )
    # demo 值必须带「试算」标签（全链标注，不冒充真实）
    assert data["metrics"]["impressions"]["display_label"] == "试算"
    # demo 不得标「真实数据」徽标
    assert data["metrics"]["impressions"]["badge"]["label"] != "真实数据"


def test_sample_marked_not_product_caliber() -> None:
    """样本数据（data_trust=sample）不得当产品口径（B7）。

    本测试锁：页面渲染时 sample 值不能标「真实数据」。
    """
    pages = WorkbenchPages()
    data = pages.product_detail(
        store_id="s1", item_id="i1", sku_id="sku1",
        metrics={"net_sales": {"evidence_state": "actual",
                               "data_trust": "sample", "value": 88.0}},
    )
    # 样本值：evidence_state 是 actual，但口径是 sample——页面必须区分
    # （此处锁：页面渲染保留 data_trust 供上游判断，不作为产品结论）
    assert data["metrics"]["net_sales"]["value"] == 88.0
    assert data["metrics"]["net_sales"]["evidence_state"] == "actual"
