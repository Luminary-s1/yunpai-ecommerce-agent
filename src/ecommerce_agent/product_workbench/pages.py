"""M9-R WP4 工作台页面路由：扩展现有 /admin（不重新设计前端）。

边界声明：
- 输入：store_id / item_id / sku_id + scope（operational 默认，对齐现有 admin）。
- 输出：只读页面数据（商品/SKU 下钻、漏斗、诊断、建议、来源、四态徽标）。
- 副作用：零——纯读，复用现有 session_scope_condition 范围隔离；无隐式写。
- 复用边界：复用 admin.py 的 scope 隔离；不重新设计前端；页面数据确定性组装。
- 失败暴露：scope 未知 → 抛；缺 id 字段 → 抛。
"""
from __future__ import annotations

from typing import Any, Mapping

from .boundaries import BOUNDARY_NOTES, DEMO_LABEL, state_badge


class WorkbenchPages:
    """商品经营工作台页面数据组装（只读）。

    用法：
      pages = WorkbenchPages()
      data = pages.product_detail(store_id="s1", item_id="i1", sku_id="sku1")
    """

    # 允许的 scope（对齐现有 admin：operational/simulation/evaluation/all）
    ALLOWED_SCOPES: frozenset[str] = frozenset(
        {"operational", "simulation", "evaluation", "all"}
    )

    def product_detail(
        self,
        *,
        store_id: str,
        item_id: str,
        sku_id: str,
        scope: str = "operational",
        metrics: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """SKU 下钻页面数据（含四态徽标 + 来源 + 时间 + 边界说明）。

        metrics 形如：{"impressions": {"evidence_state": "actual", ...}, ...}
        """
        if scope not in self.ALLOWED_SCOPES:
            raise ValueError(f"unknown_scope:{scope}")
        if not store_id or not item_id or not sku_id:
            raise ValueError("product_detail_requires_ids")
        metric_views: dict[str, Any] = {}
        for field, metric in (metrics or {}).items():
            evidence_state = metric.get("evidence_state")
            badge = state_badge(evidence_state)  # 未知状态抛（防漏标注）
            metric_views[field] = {
                "evidence_state": evidence_state,
                "badge": badge,
                "source": metric.get("source"),
                "data_as_of": metric.get("data_as_of"),
                "value": metric.get("value"),
                # demo 数据必须显式标注「试算」（对齐显示原则）
                "display_label": DEMO_LABEL if evidence_state == "demo" else None,
            }
        return {
            "store_id": store_id,
            "item_id": item_id,
            "sku_id": sku_id,
            "scope": scope,
            "metrics": metric_views,
            "boundary_notes": BOUNDARY_NOTES,
        }


__all__ = [
    "WorkbenchPages",
]
