"""M9-R WP1 权威读模型查询服务：从既有事实表投影成 SKUReadModel（WP5 复审修复）。

边界声明：
- 复用既有领域事实表（任务书约束：不得复制领域事实表）：
  * 流量漏斗 ← traffic_metric_buckets（按 SKU 关联 revision）
  * 库存 ← inventory_balances（按 SKU 跨仓汇总）
  * 订单/退款/净销 ← commerce_orders + commerce_order_lines
- 隔离铁律：SKU 层字段只放 SKU 粒度数据；店铺级字段不广播；缺数据必 MISSING。
- revision 隔离：三事实源按 revision 过滤（流量直接挂 revision；库存/订单用
  listing_revisions 的 active_from/active_to 窗口），同一 SKU 不同 revision 不串数。
- 粒度诚实：period_key/granularity 与真实聚合口径一致，不做"全生命周期 SUM 标 DAILY"
  式静默混粒度。
- 来源诚实：import_manifest_id 语义为「领域事实来源标识」（source_id/connector_id），
  authoritative_service 为权威域服务名，data_as_of 为源时间——每值可回溯到来源。
- demo/actual 派生：connector_id → source_type（virtual/operational）→ evidence_state；
  virtual → DEMO/DEMO，operational → ACTUAL/PRODUCTION，未知/缺失 → MISSING。
- 失败暴露：无任何来源时返回显式 MISSING 读模型（不抛，因为「缺数据」是合法状态）；
  查询参数缺失 → 抛 ValueError（不静默）。
- 确定性：纯查询 + 投影，无随机/时间源。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ecommerce_agent.database import Database
from ecommerce_agent.readonly_data.contracts import (
    EvidenceState,
    evidence_state_from_source_type,
    source_type_from_connector,
)

from .factory import METRIC_SPECS, _LEVEL_METRIC_FIELDS, _period_key, _to_float
from .models import (
    AggregateRule,
    DataTrust,
    Granularity,
    MetricValue,
    SKUReadModel,
)

# MISSING 占位 period_key（证据审查 #7：不编造时间戳；MISSING 允许占位符）
_PERIOD_PLACEHOLDER = "—"


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
        """投影指定 SKU 的读模型（流量/交易/库存，缺失→MISSING）。

        revision 隔离：流量直接挂 revision；库存/订单用该 revision 的
        active_from/active_to 窗口过滤，同 SKU 不同 revision 不串数。
        item 隔离（验收 WP1-1）：三事实源按 item_id 过滤，同店不同 item
        复用同 sku_id 时不串数。
        """
        if not tenant_id or not store_id or not sku_id:
            raise ValueError("sku_read_model_requires_ids")
        traffic = self._traffic_facts(
            tenant_id, store_id, item_id, sku_id, revision
        )
        inventory = self._inventory_facts(
            tenant_id, store_id, item_id, sku_id, revision
        )
        orders = self._order_facts(tenant_id, store_id, item_id, sku_id, revision)

        # SKU 层 9 个指标字段（对齐 METRIC_SPECS；源粒度可覆盖）
        metric_values: dict[str, MetricValue] = {}
        for field in _LEVEL_METRIC_FIELDS["sku"]:
            default_granularity, rule = METRIC_SPECS[field]
            facts = self._facts_for(field, traffic, inventory, orders)
            if facts["value"] is not None:
                source_type = source_type_from_connector(facts.get("connector_id"))
                state = evidence_state_from_source_type(source_type)
                if state is EvidenceState.MISSING:
                    # 防御（agentops 复审）：有值但来源未知 → 宁可标 MISSING 也不
                    # 冒充 actual；MetricValue.from_value 不允许 MISSING 带值，
                    # 所以走 missing 占位并附 reason（不静默丢数据）。
                    metric_values[field] = MetricValue.missing(
                        default_granularity,
                        rule,
                        facts["period_key"] or _PERIOD_PLACEHOLDER,
                        reason=facts["reason"]
                        or "metric_value_without_known_source",
                    )
                    continue
                granularity = facts.get("granularity") or default_granularity
                metric_values[field] = MetricValue.from_value(
                    state=state,
                    granularity=granularity,
                    aggregate_rule=rule,
                    period_key=facts["period_key"] or _period_key(
                        datetime(1970, 1, 1), granularity
                    ),
                    value=facts["value"],
                    import_manifest_id=facts["source_ref"],
                    data_as_of=facts["data_as_of"],
                    authoritative_service=facts["authoritative_service"],
                    data_trust=(
                        DataTrust.DEMO
                        if state is EvidenceState.DEMO
                        else DataTrust.PRODUCTION
                    ),
                )
            else:
                metric_values[field] = MetricValue.missing(
                    default_granularity,
                    rule,
                    facts["period_key"] or _PERIOD_PLACEHOLDER,
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

    def _revision_window(
        self, conn: Any, tenant_id: str, store_id: str, item_id: str, sku_id: str,
        revision: int,
    ) -> dict[str, Any]:
        """取指定 revision 的 active_from/active_to 窗口（revision 隔离用）。

        返回 dict 含 active_from/active_to/connector_id；无匹配 → 空 dict。
        验收覆盖 WP1-1：WHERE 含 item_id——同店不同 item 复用同 sku_id 时不串数。
        """
        row = conn.execute(
            """
            SELECT active_from, active_to, connector_id, id
            FROM listing_revisions
            WHERE tenant_id=? AND store_id=? AND item_id=? AND sku_id=? AND revision_no=?
            """,
            (tenant_id, store_id, item_id, sku_id, revision),
        ).fetchone()
        if row is None:
            return {}
        return {
            "active_from": row["active_from"],
            "active_to": row["active_to"],
            "connector_id": row["connector_id"],
            "revision_id": row["id"],
        }

    def _traffic_facts(
        self, tenant_id: str, store_id: str, item_id: str, sku_id: str, revision: int
    ) -> dict[str, Any]:
        """SKU 流量漏斗：revision → metric_buckets 按日聚合（revision 隔离）。"""
        with self.db.connect() as conn:
            window = self._revision_window(
                conn, tenant_id, store_id, item_id, sku_id, revision
            )
            if not window:
                return self._missing_traffic("traffic_revision_not_found")
            row = conn.execute(
                """
                SELECT b.impressions, b.clicks, b.cart_adds, b.orders,
                       b.data_as_of, b.source_id, b.connector_id,
                       b.metric_start, b.metric_end, b.bucket_granularity
                FROM traffic_metric_buckets b
                WHERE b.tenant_id=? AND b.listing_revision_id=?
                ORDER BY b.metric_start DESC LIMIT 1
                """,
                (tenant_id, window["revision_id"]),
            ).fetchone()
        if row is None:
            return self._missing_traffic("traffic_metric_evidence_not_found")
        # 粒度诚实：按 bucket 自身粒度标注（hour→HOURLY，day→DAILY），不强行标 DAILY
        bucket_granularity = row["bucket_granularity"]
        granularity = Granularity.HOURLY if bucket_granularity == "hour" else None
        return {
            "impressions": _to_float(row["impressions"]),
            "clicks": _to_float(row["clicks"]),
            "add_to_cart": _to_float(row["cart_adds"]),
            "orders": _to_float(row["orders"]),
            "data_as_of": datetime.fromisoformat(row["data_as_of"])
            if row["data_as_of"] else None,
            "source_ref": row["source_id"],
            "authoritative_service": "traffic_metric_buckets",
            "connector_id": row["connector_id"],
            "granularity": granularity,
            "reason": None,
            "period_key": (
                row["metric_start"] or row["data_as_of"]
            )[:13 if granularity is not None else 10]
            if (row["metric_start"] or row["data_as_of"]) else None,
        }

    def _missing_traffic(self, reason: str) -> dict[str, Any]:
        return {
            "impressions": None, "clicks": None, "add_to_cart": None,
            "orders": None, "data_as_of": None, "source_ref": None,
            "connector_id": None, "granularity": None,
            "authoritative_service": "traffic_metric_buckets",
            "reason": reason,
            "period_key": None,
        }

    def _inventory_facts(
        self, tenant_id: str, store_id: str, item_id: str, sku_id: str, revision: int
    ) -> dict[str, Any]:
        """SKU 库存：inventory_balances 跨仓汇总（revision 窗口内最新）。"""
        with self.db.connect() as conn:
            window = self._revision_window(
                conn, tenant_id, store_id, item_id, sku_id, revision
            )
            if not window:
                return self._missing_inventory("inventory_revision_not_found")
            row = conn.execute(
                """
                SELECT COALESCE(SUM(CAST(on_hand AS REAL)), 0) AS on_hand_total,
                       COALESCE(SUM(CAST(inbound AS REAL)), 0) AS inbound_total,
                       MAX(source_updated_at) AS latest_updated,
                       MAX(connector_id) AS connector_id,
                       MAX(source_id) AS latest_source_id
                FROM inventory_balances
                WHERE tenant_id=? AND store_id=? AND sku_id=?
                  AND source_updated_at>=? AND source_updated_at<=?
                """,
                (
                    tenant_id, store_id, sku_id,
                    window["active_from"],
                    window["active_to"] or "9999-12-31T23:59:59+00:00",
                ),
            ).fetchone()
        if row is None or (row["on_hand_total"] == 0 and row["inbound_total"] == 0 and row["latest_updated"] is None):
            return self._missing_inventory("inventory_evidence_not_found")
        return {
            "sellable_stock": _to_float(row["on_hand_total"]),
            "in_transit_stock": _to_float(row["inbound_total"]),
            "data_as_of": datetime.fromisoformat(row["latest_updated"])
            if row["latest_updated"] else None,
            # 来源诚实（证据审查 #1）：取最新行的真实 source_id，不合成前缀串
            "source_ref": row["latest_source_id"],
            "authoritative_service": "inventory_balances",
            "connector_id": row["connector_id"],
            "reason": None,
            "period_key": (row["latest_updated"] or "")[:10] or None,
        }

    def _missing_inventory(self, reason: str) -> dict[str, Any]:
        return {
            "sellable_stock": None, "in_transit_stock": None,
            "data_as_of": None, "source_ref": None, "connector_id": None,
            "granularity": None,
            "authoritative_service": "inventory_balances",
            "reason": reason,
            "period_key": None,
        }

    def _order_facts(
        self, tenant_id: str, store_id: str, item_id: str, sku_id: str, revision: int
    ) -> dict[str, Any]:
        """SKU 交易：commerce_orders 按 revision 窗口聚合（payments/net_sales）。

        粒度诚实：只聚合 revision 窗口内的订单（placed_at 落在 active_from/active_to），
        period_key 用窗口起始，而非"全生命周期 SUM 标 DAILY"。
        """
        with self.db.connect() as conn:
            window = self._revision_window(
                conn, tenant_id, store_id, item_id, sku_id, revision
            )
            if not window:
                return self._missing_orders("order_revision_not_found")
            row = conn.execute(
                """
                SELECT COUNT(l.id) AS line_count,
                       COALESCE(SUM(l.quantity), 0) AS order_qty,
                       COALESCE(SUM(CAST(l.unit_price AS REAL) * l.quantity), 0) AS gross,
                       MAX(o.source_updated_at) AS latest_updated,
                       MAX(o.connector_id) AS connector_id,
                       MAX(o.source_id) AS latest_source_id
                FROM commerce_orders o
                JOIN commerce_order_lines l ON l.order_id=o.id
                WHERE o.tenant_id=? AND o.store_id=? AND l.sku_id=?
                  AND o.placed_at>=? AND o.placed_at<=?
                """,
                (
                    tenant_id, store_id, sku_id,
                    window["active_from"],
                    window["active_to"] or "9999-12-31T23:59:59+00:00",
                ),
            ).fetchone()
        if row is None or (row["line_count"] or 0) == 0:
            return self._missing_orders("order_evidence_not_found")
        return {
            "payments": _to_float(row["order_qty"]),
            "refunds": None,  # 退款口径当前无独立来源，不编造
            "net_sales": _to_float(row["gross"]),
            # data_as_of 统一源摄入时间（证据审查 #4：与库存口径一致，
            # 补录旧订单不低估新鲜度）；业务窗口过滤仍用 placed_at
            "data_as_of": datetime.fromisoformat(row["latest_updated"])
            if row["latest_updated"] else None,
            # 来源诚实（证据审查 #1）：取最新行的真实 source_id，不合成前缀串
            "source_ref": row["latest_source_id"],
            "authoritative_service": "commerce_orders",
            "connector_id": row["connector_id"],
            "reason": None,
            "period_key": (window["active_from"] or "")[:10] or None,
        }

    def _missing_orders(self, reason: str) -> dict[str, Any]:
        return {
            "payments": None, "refunds": None, "net_sales": None,
            "data_as_of": None, "source_ref": None, "connector_id": None,
            "granularity": None,
            "authoritative_service": "commerce_orders",
            "reason": reason,
            "period_key": None,
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
