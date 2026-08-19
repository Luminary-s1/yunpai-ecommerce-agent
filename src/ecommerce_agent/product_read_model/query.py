"""M9-R WP1 权威读模型查询服务：从既有事实表投影成 SKUReadModel（WP5 验收修复）。

边界声明：
- 复用既有领域事实表（任务书约束：不得复制领域事实表）：
  * 流量漏斗 ← traffic_metric_buckets（按 SKU 关联 revision）
  * 库存 ← inventory_balances
  * 订单/退款/净销 ← commerce_orders + commerce_order_lines
- 隔离铁律：SKU 层字段只放 SKU 粒度数据；店铺级字段不广播；缺数据必 MISSING。
- 失败暴露：无任何来源时返回显式 MISSING 读模型（不抛，因为「缺数据」是合法状态）；
  查询参数缺失 → 抛 ValueError（不静默）。
- 确定性：纯查询 + 投影，无随机/时间源。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ecommerce_agent.database import Database
from ecommerce_agent.readonly_data.contracts import EvidenceState

from .factory import METRIC_SPECS, _LEVEL_METRIC_FIELDS, _period_key, _to_float
from .models import (
    AggregateRule,
    DataTrust,
    Granularity,
    MetricValue,
    SKUReadModel,
)

# 确定性 period_key 兜底（仅格式契约，不承载语义；真实数据优先）
_FALLBACK_PERIOD = datetime(1970, 1, 1)


class ProductReadQuery:
    """从真实事实表查询并投影 SKU 读模型（权威查询能力）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def sku_read_model(
        self,
        tenant_id: str,
        *,
        store_id: str,
        item_id: str,
        sku_id: str,
        revision: int = 1,
    ) -> SKUReadModel:
        """投影指定 SKU 的读模型（流量/交易/库存，缺失→MISSING）。"""
        if not tenant_id or not store_id or not sku_id:
            raise ValueError("sku_read_model_requires_ids")
        traffic = self._traffic_facts(tenant_id, store_id, sku_id)
        inventory = self._inventory_facts(tenant_id, store_id, sku_id)
        orders = self._order_facts(tenant_id, store_id, sku_id)

        # SKU 层 9 个指标字段（对齐 METRIC_SPECS）
        metric_values: dict[str, MetricValue] = {}
        for field in _LEVEL_METRIC_FIELDS["sku"]:
            granularity, rule = METRIC_SPECS[field]
            facts = self._facts_for(field, traffic, inventory, orders)
            if facts["value"] is not None:
                metric_values[field] = MetricValue.from_value(
                    state=EvidenceState.ACTUAL,
                    granularity=granularity,
                    aggregate_rule=rule,
                    period_key=facts["period_key"] or _period_key(
                        _FALLBACK_PERIOD, granularity
                    ),
                    value=facts["value"],
                    import_manifest_id=facts["source_ref"],
                    data_as_of=facts["data_as_of"],
                    authoritative_service=facts["authoritative_service"],
                    data_trust=DataTrust.PRODUCTION,
                )
            else:
                metric_values[field] = MetricValue.missing(
                    granularity,
                    rule,
                    facts["period_key"]
                    or _period_key(_FALLBACK_PERIOD, granularity),
                    reason=facts["reason"] or "field_not_in_row",
                )
        return SKUReadModel(
            tenant_id=tenant_id,
            store_id=store_id,
            item_id=item_id,
            sku_id=sku_id,
            revision=revision,
            impressions=metric_values["impressions"],
            clicks=metric_values["clicks"],
            add_to_cart=metric_values["add_to_cart"],
            orders=metric_values["orders"],
            payments=metric_values["payments"],
            refunds=metric_values["refunds"],
            net_sales=metric_values["net_sales"],
            sellable_stock=metric_values["sellable_stock"],
            in_transit_stock=metric_values["in_transit_stock"],
        )

    # ── 内部：各事实源聚合 ──

    def _traffic_facts(
        self, tenant_id: str, store_id: str, sku_id: str
    ) -> dict[str, Any]:
        """SKU 流量漏斗：revision → metric_buckets 聚合。"""
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT b.impressions, b.clicks, b.cart_adds, b.orders,
                       b.data_as_of, b.source_id
                FROM traffic_metric_buckets b
                JOIN listing_revisions r ON r.tenant_id=b.tenant_id
                    AND r.id=b.listing_revision_id
                WHERE b.tenant_id=? AND r.store_id=? AND r.sku_id=?
                ORDER BY b.metric_start DESC LIMIT 1
                """,
                (tenant_id, store_id, sku_id),
            ).fetchone()
        if row is None:
            return {
                "impressions": None, "clicks": None, "add_to_cart": None,
                "orders": None, "data_as_of": None, "source_ref": None,
                "authoritative_service": "traffic_metric_buckets",
                "reason": "traffic_metric_evidence_not_found",
                "period_key": None,
            }
        return {
            "impressions": _to_float(row["impressions"]),
            "clicks": _to_float(row["clicks"]),
            "add_to_cart": _to_float(row["cart_adds"]),
            "orders": _to_float(row["orders"]),
            "data_as_of": datetime.fromisoformat(row["data_as_of"])
            if row["data_as_of"] else None,
            "source_ref": row["source_id"],
            "authoritative_service": "traffic_metric_buckets",
            "reason": None,
            "period_key": row["data_as_of"][:10] if row["data_as_of"] else None,
        }

    def _inventory_facts(
        self, tenant_id: str, store_id: str, sku_id: str
    ) -> dict[str, Any]:
        """SKU 库存：inventory_balances 最新 on_hand/inbound。"""
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT on_hand, inbound, source_updated_at, connector_id
                FROM inventory_balances
                WHERE tenant_id=? AND store_id=? AND sku_id=?
                ORDER BY source_updated_at DESC LIMIT 1
                """,
                (tenant_id, store_id, sku_id),
            ).fetchone()
        if row is None:
            return {
                "sellable_stock": None, "in_transit_stock": None,
                "data_as_of": None, "source_ref": None,
                "authoritative_service": "inventory_balances",
                "reason": "inventory_evidence_not_found",
                "period_key": None,
            }
        return {
            "sellable_stock": _to_float(row["on_hand"]),
            "in_transit_stock": _to_float(row["inbound"]),
            "data_as_of": datetime.fromisoformat(row["source_updated_at"])
            if row["source_updated_at"] else None,
            "source_ref": row["connector_id"],
            "authoritative_service": "inventory_balances",
            "reason": None,
            "period_key": row["source_updated_at"][:10]
            if row["source_updated_at"] else None,
        }

    def _order_facts(
        self, tenant_id: str, store_id: str, sku_id: str
    ) -> dict[str, Any]:
        """SKU 交易：commerce_orders 按行聚合（payments/refunds/net_sales）。"""
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(l.id) AS line_count,
                       COALESCE(SUM(l.quantity), 0) AS order_qty,
                       COALESCE(SUM(CAST(l.unit_price AS REAL) * l.quantity), 0) AS gross,
                       MAX(o.placed_at) AS latest_placed_at
                FROM commerce_orders o
                JOIN commerce_order_lines l ON l.order_id=o.id
                WHERE o.tenant_id=? AND o.store_id=? AND l.sku_id=?
                """,
                (tenant_id, store_id, sku_id),
            ).fetchone()
        if row is None or (row["line_count"] or 0) == 0:
            return {
                "payments": None, "refunds": None, "net_sales": None,
                "data_as_of": None, "source_ref": None,
                "authoritative_service": "commerce_orders",
                "reason": "order_evidence_not_found",
                "period_key": None,
            }
        return {
            "payments": _to_float(row["order_qty"]),
            "refunds": None,  # 退款口径当前无独立来源，不编造
            "net_sales": _to_float(row["gross"]),
            "data_as_of": datetime.fromisoformat(row["latest_placed_at"])
            if row["latest_placed_at"] else None,
            "source_ref": "commerce_orders",
            "authoritative_service": "commerce_orders",
            "reason": None,
            "period_key": row["latest_placed_at"][:10]
            if row["latest_placed_at"] else None,
        }

    @staticmethod
    def _facts_for(
        field: str,
        traffic: dict[str, Any],
        inventory: dict[str, Any],
        orders: dict[str, Any],
    ) -> dict[str, Any]:
        """按字段从对应事实源取投影（确定性）。返回统一 shape 含 value。"""
        if field in ("impressions", "clicks", "add_to_cart", "orders"):
            source = traffic
        elif field in ("sellable_stock", "in_transit_stock"):
            source = inventory
        else:
            source = orders
        # 源 dict 用字段名存值；统一补 value 键给投影用
        result = dict(source)
        result["value"] = source.get(field)
        return result


__all__ = [
    "ProductReadQuery",
]
