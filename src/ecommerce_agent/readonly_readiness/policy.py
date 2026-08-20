from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from ..readonly_data import REPORT_ADAPTERS


READINESS_POLICY_VERSION = "readonly-readiness-v1"


@dataclass(frozen=True, slots=True)
class ReadinessReportPolicy:
    report_type: str
    max_age_hours: int


@dataclass(frozen=True, slots=True)
class ReadinessGapRequirement:
    field_key: str
    label: str
    reason: str
    impact: str


# Freshness thresholds are management-view policy, not adapter parsing rules.
# Adapter domain, grain, mapping version and units remain authoritative in WP2.
READINESS_REPORT_POLICIES: Mapping[str, ReadinessReportPolicy] = MappingProxyType(
    {
        "catalog_snapshot": ReadinessReportPolicy("catalog_snapshot", 72),
        "inventory_snapshot": ReadinessReportPolicy("inventory_snapshot", 24),
        "order_snapshot": ReadinessReportPolicy("order_snapshot", 48),
        "fulfillment_snapshot": ReadinessReportPolicy("fulfillment_snapshot", 24),
        "operations_daily": ReadinessReportPolicy("operations_daily", 48),
        "marketing_daily": ReadinessReportPolicy("marketing_daily", 48),
        "refund_snapshot": ReadinessReportPolicy("refund_snapshot", 48),
        "settlement_statement": ReadinessReportPolicy(
            "settlement_statement", 24 * 35
        ),
    }
)


READINESS_GAP_REQUIREMENTS: Mapping[str, ReadinessGapRequirement] = MappingProxyType(
    {
        "purchase_cost": ReadinessGapRequirement(
            field_key="purchase_cost",
            label="进货成本",
            reason="purchase_cost_source_missing",
            impact="无法计算包含进货成本的完整经营利润",
        ),
        "purchase_order": ReadinessGapRequirement(
            field_key="purchase_order",
            label="采购单",
            reason="purchase_order_source_missing",
            impact="无法核验采购批次与到货履约",
        ),
        "transport_cycle": ReadinessGapRequirement(
            field_key="transport_cycle",
            label="运输周期",
            reason="transport_cycle_source_missing",
            impact="无法形成供应链补货提前期证据",
        ),
        "refurbishment_cost": ReadinessGapRequirement(
            field_key="refurbishment_cost",
            label="翻新成本",
            reason="refurbishment_cost_source_missing",
            impact="无法完整归集退货商品处置成本",
        ),
    }
)


_adapter_report_types = {adapter.report_type for adapter in REPORT_ADAPTERS.list()}
if set(READINESS_REPORT_POLICIES) != _adapter_report_types:
    missing = sorted(_adapter_report_types - set(READINESS_REPORT_POLICIES))
    extra = sorted(set(READINESS_REPORT_POLICIES) - _adapter_report_types)
    raise RuntimeError(
        f"readonly_readiness_policy_mismatch:missing={missing}:extra={extra}"
    )


__all__ = [
    "READINESS_GAP_REQUIREMENTS",
    "READINESS_POLICY_VERSION",
    "READINESS_REPORT_POLICIES",
    "ReadinessGapRequirement",
    "ReadinessReportPolicy",
]
