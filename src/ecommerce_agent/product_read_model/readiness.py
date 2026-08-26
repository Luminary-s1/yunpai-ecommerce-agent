"""M9-R WP1 数据准备度：评估读模型的缺失/可用性状态。

边界声明：
- 纯派生模块：输入 SKUReadModel（不可变），输出 SKUReadiness（不可变）。
- 无 I/O、无副作用、无时间源、无随机。
- 唯一证据状态枚举 = M7-R EvidenceState；漏斗可用性 funnel_availability 是它的
  派生视图（"complete"/"partial"/"unavailable"），不是第二套状态标签。
- 派生规则确定性：任一漏斗字段非 MISSING 计可用；全可用=complete；
  部分=partial；全 MISSING=unavailable。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ecommerce_agent.readonly_data.contracts import EvidenceState

from .models import AggregateRule, Granularity, SKUReadModel

# 漏斗关键字段（顺序即漏斗顺序：曝光→点击→加购→支付→退款→净销，对齐任务书漏斗）
FUNNEL_FIELDS: tuple[str, ...] = (
    "impressions",
    "clicks",
    "add_to_cart",
    "orders",
    "payments",
    "refunds",
    "net_sales",
)

# 派生值约定（字符串，非枚举——避免第二套状态标签）
FUNNEL_COMPLETE = "complete"
FUNNEL_PARTIAL = "partial"
FUNNEL_UNAVAILABLE = "unavailable"


class MetricReadiness(BaseModel):
    """单指标就绪描述（不可变）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_key: str
    evidence_state: EvidenceState
    granularity: Granularity
    aggregate_rule: AggregateRule
    reason: str | None = None


class SKUReadiness(BaseModel):
    """SKU 指标就绪矩阵 + 派生漏斗可用性（不可变）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    composite_key: tuple[str, str, str, str, int]
    metrics: tuple[MetricReadiness, ...]
    funnel_availability: str  # complete / partial / unavailable（派生值）

    def readiness_for(self, field_key: str) -> MetricReadiness | None:
        """按字段名取就绪项；不存在返回 None（显式，不静默）。"""
        for metric in self.metrics:
            if metric.field_key == field_key:
                return metric
        return None


def build_sku_readiness(sku: SKUReadModel) -> SKUReadiness:
    """从 SKUReadModel 计算准备度矩阵与漏斗可用性（确定性派生）。

    输入：sku（SKUReadModel，含 9 个 MetricValue 字段）。
    输出：SKUReadiness（frozen）。
    副作用：零。
    """
    observed_fields: tuple[str, ...] = FUNNEL_FIELDS + ("sellable_stock", "in_transit_stock")
    metrics: list[MetricReadiness] = []
    funnel_available = 0
    for field in observed_fields:
        metric_value = getattr(sku, field)
        metrics.append(
            MetricReadiness(
                field_key=field,
                evidence_state=metric_value.evidence_state,
                granularity=metric_value.granularity,
                aggregate_rule=metric_value.aggregate_rule,
                reason=metric_value.reason,
            )
        )
        # 漏斗可用性只按 FUNNEL_FIELDS 判定（库存字段不属于漏斗，不参与）
        if field in FUNNEL_FIELDS and metric_value.evidence_state is not EvidenceState.MISSING:
            funnel_available += 1
    if funnel_available == 0:
        availability = FUNNEL_UNAVAILABLE
    elif funnel_available == len(FUNNEL_FIELDS):
        availability = FUNNEL_COMPLETE
    else:
        availability = FUNNEL_PARTIAL
    return SKUReadiness(
        composite_key=sku.composite_key(),
        metrics=tuple(metrics),
        funnel_availability=availability,
    )


__all__ = [
    "FUNNEL_COMPLETE",
    "FUNNEL_FIELDS",
    "FUNNEL_PARTIAL",
    "FUNNEL_UNAVAILABLE",
    "MetricReadiness",
    "SKUReadiness",
    "build_sku_readiness",
]
