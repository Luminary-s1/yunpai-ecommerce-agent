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
    ListingRevisionEvidence,
    MetricValue,
    ProductIdentityEvidence,
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
        identity = self._product_identity(
            tenant_id, store_id, item_id, sku_id
        )
        listing_revision = self._listing_revision_evidence(
            tenant_id, store_id, item_id, sku_id, revision
        )

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
            listing_revision=listing_revision,
            material_code=identity["material_code"] if identity else None,
            product_identity_evidence=(
                identity["evidence"] if identity else None
            ),
            title=identity["title"] if identity else None,
            merchant_code=identity["merchant_code"] if identity else None,
            impressions=metric_values["impressions"],
            clicks=metric_values["clicks"],
            add_to_cart=metric_values["add_to_cart"],
            orders=metric_values["orders"],
            payments=metric_values["payments"],
            refunds=metric_values["refunds"],
            net_sales=metric_values["net_sales"],
            sellable_stock=metric_values["sellable_stock"],
            in_transit_stock=metric_values["in_transit_stock"],
            ad_spend=self._ad_spend_missing(),
            competitor_price=self._competitor_price(tenant_id, store_id, sku_id),
            experiment_state=self._experiment_missing(),
        )

    # ── 内部：各事实源聚合 ──

    def _sku_item_count(
        self, conn: Any, tenant_id: str, store_id: str, sku_id: str
    ) -> int:
        """该 SKU 在 store 下的不同 item 数（R1 方案 B：显式共享语义）。

        判断同 SKU 是否单 item：单 item 时 NULL 共享行可投影（保留真实粒度）；
        多 item 时 NULL 行不广播（MISSING + reason），避免串数（任务书 L140/L142）。
        """
        row = conn.execute(
            "SELECT COUNT(DISTINCT item_id) AS c FROM listing_revisions "
            "WHERE tenant_id=? AND store_id=? AND sku_id=? AND item_id IS NOT NULL",
            (tenant_id, store_id, sku_id),
        ).fetchone()
        return int(row["c"]) if row else 0

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
            SELECT active_from, active_to, connector_id, id, revision_no,
                   source_updated_at
            FROM listing_revisions
            WHERE tenant_id=? AND store_id=? AND item_id=? AND sku_id=? AND revision_no=?
            -- A4（盲点 #4 修复）：listing_revisions UNIQUE 含 connector_id——同
            -- (store,item,sku,revision_no) 多 connector 各一行合法。加确定性 ORDER BY
            -- （source_updated_at 最新 + id 尾键），避免 fetchone() 任取一行
            ORDER BY source_updated_at DESC, id DESC
            LIMIT 1
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
            "revision_no": row["revision_no"],
            "source_updated_at": row["source_updated_at"],
            "item_id": item_id,  # P1: 窗口消费侧透传 item_id，聚合按链接过滤
        }

    def _listing_revision_evidence(
        self,
        tenant_id: str,
        store_id: str,
        item_id: str,
        sku_id: str,
        revision: int,
    ) -> ListingRevisionEvidence | None:
        with self.db.connect() as conn:
            window = self._revision_window(
                conn, tenant_id, store_id, item_id, sku_id, revision
            )
        if not window:
            return None
        return ListingRevisionEvidence(
            revision_id=str(window["revision_id"]),
            revision_no=int(window["revision_no"]),
            connector_id=str(window["connector_id"]),
            active_from=datetime.fromisoformat(str(window["active_from"])),
            active_to=(
                datetime.fromisoformat(str(window["active_to"]))
                if window["active_to"] is not None else None
            ),
            source_updated_at=datetime.fromisoformat(
                str(window["source_updated_at"])
            ),
        )

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
            rows = conn.execute(
                """
                SELECT b.impressions, b.clicks, b.cart_adds, b.orders,
                       b.data_as_of, b.source_id, b.connector_id,
                       b.metric_start, b.metric_end, b.bucket_granularity
                FROM traffic_metric_buckets b
                WHERE b.tenant_id=? AND b.listing_revision_id=?
                  AND b.metric_start=(
                      SELECT MAX(latest.metric_start)
                      FROM traffic_metric_buckets latest
                      WHERE latest.tenant_id=b.tenant_id
                        AND latest.listing_revision_id=b.listing_revision_id
                  )
                ORDER BY b.id
                """,
                (tenant_id, window["revision_id"]),
            ).fetchall()
        if not rows:
            return self._missing_traffic("traffic_metric_evidence_not_found")
        if len(rows) != 1:
            # M5-R has not frozen traffic-source aggregation semantics. Summing can
            # double count an overall row plus channel rows; choosing one understates
            # the SKU. Fail closed until an authoritative breakdown contract exists.
            return self._missing_traffic(
                "traffic_source_breakdown_requires_explicit_aggregation"
            )
        row = rows[0]
        # 粒度诚实：按 bucket 自身粒度标注（hour→HOURLY，day→DAILY）。
        # 未知粒度（非 hour/day）→ 显式 MISSING + reason，不回落默认 DAILY 冒充
        # 日粒度——对齐任务书「不同粒度不得静默相加/标错」（当前表结构 CHECK 只允许
        # hour/day，未知粒度仅来自迁移或手工插库，属防御纵深）。
        bucket_granularity = row["bucket_granularity"]
        if bucket_granularity not in ("hour", "day"):
            return self._missing_traffic("traffic_granularity_unsupported")
        granularity = (
            Granularity.HOURLY if bucket_granularity == "hour" else Granularity.DAILY
        )
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
            )[:13 if bucket_granularity == "hour" else 10]
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
            # R1 方案 B（显式共享语义）：单 item 时 NULL 共享行可投影（保留真实粒度，
            # 任务书 L43）；多 item 时 NULL 行不广播（MISSING，任务书 L140/L142 不串数）。
            single_item = self._sku_item_count(conn, tenant_id, store_id, sku_id) <= 1
            item_cond = "(ib.item_id=? OR ib.item_id IS NULL)" if single_item else "ib.item_id=?"
            cte_item_cond = "(item_id=? OR item_id IS NULL)" if single_item else "item_id=?"
            row = conn.execute(
                f"""
                -- R1/R2 来源同源（确定性）：connector_id/source_id 来自 source_updated_at
                -- 最新的一行（全局 ORDER BY + 唯一尾键 + LIMIT 1 取整行），禁止分区 rn=1
                -- 多行任取、禁止分别 MAX 拼凑（那会产生数据库中不存在的组合）。
                WITH latest AS (
                    SELECT connector_id, source_id
                    FROM inventory_balances
                    WHERE tenant_id=? AND store_id=? AND sku_id=? AND {cte_item_cond}
                      AND source_updated_at>=? AND source_updated_at<=?
                    ORDER BY source_updated_at DESC, version DESC, id DESC
                    LIMIT 1
                )
                SELECT COALESCE(SUM(CAST(on_hand AS REAL)), 0) AS on_hand_total,
                       COALESCE(SUM(CAST(inbound AS REAL)), 0) AS inbound_total,
                       MAX(source_updated_at) AS latest_updated,
                       (SELECT connector_id FROM latest LIMIT 1) AS connector_id,
                       (SELECT source_id FROM latest LIMIT 1) AS latest_source_id
                FROM inventory_balances ib
                WHERE ib.tenant_id=? AND ib.store_id=? AND ib.sku_id=? AND {item_cond}
                  AND ib.source_updated_at>=? AND ib.source_updated_at<=?
                """,
                (
                    tenant_id, store_id, sku_id, window["item_id"],
                    window["active_from"],
                    window["active_to"] or "9999-12-31T23:59:59+00:00",
                    tenant_id, store_id, sku_id, window["item_id"],
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

        item 隔离按 commerce_order_lines.item_id 执行；旧行可回退到历史订单头
        commerce_orders.item_id。两处都缺失时，只有该 SKU 在 revision 中唯一对应一个
        item 才能兼容投影；存在多个候选 item 时 fail closed，不广播未知归属订单。
        """
        with self.db.connect() as conn:
            window = self._revision_window(
                conn, tenant_id, store_id, item_id, sku_id, revision
            )
            if not window:
                return self._missing_orders("order_revision_not_found")
            single_item = self._sku_item_count(conn, tenant_id, store_id, sku_id) <= 1
            legacy_line = (
                " OR (l.item_id IS NULL AND o.item_id IS NULL)" if single_item else ""
            )
            line_item_cond = f"(COALESCE(l.item_id,o.item_id)=?{legacy_line})"
            legacy_line2 = (
                " OR (l2.item_id IS NULL AND o.item_id IS NULL)" if single_item else ""
            )
            line2_item_cond = f"(COALESCE(l2.item_id,o.item_id)=?{legacy_line2})"
            row = conn.execute(
                f"""
                -- R2 来源同源（确定性）：connector_id/source_id 取自 source_updated_at
                -- 最新的一行（全局 ORDER BY + 唯一尾键 + LIMIT 1 取整行），禁止分区 rn=1
                -- 多行任取、禁止分别 MAX 拼凑（那会产生数据库中不存在的组合）。
                -- 作用域与主聚合一致：按 sku_id + 订单行 item 身份过滤。
                WITH latest AS (
                    SELECT o.connector_id, o.source_id, o.id
                    FROM commerce_orders o
                    JOIN commerce_order_lines l ON l.order_id=o.id
                    WHERE o.tenant_id=? AND o.store_id=? AND l.sku_id=?
                      AND {line_item_cond}
                      AND o.placed_at>=? AND o.placed_at<=?
                      -- A1（盲点 #7 修复）：来源行与聚合行同口径——只统计有效支付状态
                      -- 订单（canceled/unpaid 不计入 payments/gross/net_sales，任务书 L60/L66）
                      AND o.order_status IN ('paid','fulfilling','shipped','delivered')
                      AND o.payment_status IN ('paid','partially_refunded')
                    ORDER BY o.source_updated_at DESC, o.version DESC, o.id DESC
                    LIMIT 1
                )
                SELECT COUNT(l.id) AS line_count,
                       COALESCE(SUM(l.quantity), 0) AS order_qty,
                       COALESCE(SUM(CAST(l.unit_price AS REAL) * l.quantity), 0) AS gross,
                       MAX(o.source_updated_at) AS latest_updated,
                       (SELECT connector_id FROM latest LIMIT 1) AS connector_id,
                       (SELECT source_id FROM latest LIMIT 1) AS latest_source_id
                FROM commerce_orders o
                JOIN commerce_order_lines l ON l.order_id=o.id
                WHERE o.tenant_id=? AND o.store_id=? AND l.sku_id=?
                  AND {line_item_cond}
                  AND o.placed_at>=? AND o.placed_at<=?
                  -- A1（盲点 #7 修复）：只统计有效支付状态订单
                  AND o.order_status IN ('paid','fulfilling','shipped','delivered')
                  AND o.payment_status IN ('paid','partially_refunded')
                """,
                (
                    tenant_id, store_id, sku_id, window["item_id"],
                    window["active_from"],
                    window["active_to"] or "9999-12-31T23:59:59+00:00",
                    tenant_id, store_id, sku_id, window["item_id"],
                    window["active_from"],
                    window["active_to"] or "9999-12-31T23:59:59+00:00",
                ),
            ).fetchone()
            # 退款口径（G4）：退款是订单级（commerce_after_sale_cases 挂 order_id，
            # 无 SKU 维度）。只有当该 SKU 的订单都只有单行（单 SKU）时才能精确归退款；
            # 多行订单（多 SKU）的退款无法归到 SKU → 标 MISSING（reason 明确阻断），
            # 不 JOIN order_lines 重复累计（那会把整单退款放大 N 倍）。
            multi_line = conn.execute(
                f"""
                SELECT COUNT(*) AS multi
                FROM commerce_order_lines l
                JOIN commerce_orders parent ON parent.id=l.order_id
                WHERE l.order_id IN (
                    SELECT DISTINCT o.id FROM commerce_orders o
                    JOIN commerce_order_lines l2 ON l2.order_id=o.id
                    WHERE o.tenant_id=? AND o.store_id=? AND l2.sku_id=?
                      AND {line2_item_cond}
                      AND o.placed_at>=? AND o.placed_at<=?
                      -- A1：multi_line 判定同口径——只统计有效支付状态订单
                      AND o.order_status IN ('paid','fulfilling','shipped','delivered')
                      AND o.payment_status IN ('paid','partially_refunded')
                )
                -- 订单级退款只有在整单都属于同一 SKU + item 时才可归属。
                GROUP BY l.order_id
                HAVING COUNT(DISTINCT l.sku_id) > 1
                    OR COUNT(DISTINCT COALESCE(l.item_id,parent.item_id)) > 1
                    OR SUM(CASE WHEN COALESCE(l.item_id,parent.item_id) IS NULL
                                THEN 1 ELSE 0 END) > 0
                LIMIT 1
                """,
                (
                    tenant_id, store_id, sku_id, window["item_id"],
                    window["active_from"],
                    window["active_to"] or "9999-12-31T23:59:59+00:00",
                ),
            ).fetchone()
            if multi_line is not None:
                refund_value = None
                refund_reason = "refund_not_attributable_to_sku_multi_line_order"
            else:
                refund_row = conn.execute(
                    f"""
                    SELECT COALESCE(SUM(CAST(a.approved_amount AS REAL)), 0) AS refund_total
                    FROM commerce_after_sale_cases a
                    JOIN commerce_orders o ON o.id=a.order_id
                    WHERE o.tenant_id=? AND o.store_id=?
                      -- 退款是订单级（无 SKU 维度）：用 EXISTS 判断订单含该 SKU 的行，
                      -- 不 JOIN order_lines（那会把整单退款按 SKU 行数放大 N 倍——
                      -- 同 SKU 拆两行时一条退款 50 会被算成 100，负责人复验阻断项 3）
                      AND EXISTS (
                          SELECT 1 FROM commerce_order_lines l
                          WHERE l.order_id=o.id AND l.sku_id=?
                            AND {line_item_cond}
                      )
                      AND a.case_type IN ('refund','return_refund')
                      AND a.status IN ('approved','completed')
                      AND o.placed_at>=? AND o.placed_at<=?
                      -- A1（盲点 #7 修复）：退款只统计有效支付状态订单的退款
                      AND o.order_status IN ('paid','fulfilling','shipped','delivered')
                      AND o.payment_status IN ('paid','partially_refunded')
                    """,
                    (
                        tenant_id, store_id, sku_id, window["item_id"],
                        window["active_from"],
                        window["active_to"] or "9999-12-31T23:59:59+00:00",
                    ),
                ).fetchone()
                refund_value = (
                    _to_float(refund_row["refund_total"])
                    if refund_row and refund_row["refund_total"] not in (None, 0)
                    else None
                )
                refund_reason = None if refund_value is not None else "refund_source_not_available"
        if row is None or (row["line_count"] or 0) == 0:
            return self._missing_orders("order_evidence_not_found")
        # 退款：订单级金额，只有单行订单能精确归 SKU；多行订单退款无法归 SKU → MISSING。
        # reason 是字段级：refund 的 reason 独立，不污染 payments/net_sales（它们有值）。
        gross_value = _to_float(row["gross"])
        # R2（证据诚实）：net_sales = 收入 - 可归属退款，不能用 GMV 冒充净额。
        # 三态：单行有退款 → gross - refunds；单行无退款 → gross；
        # 多行订单退款无法归属 → net_sales MISSING（同 refunds 因，不能以 GMV 冒充，任务书 L66）。
        if refund_value is not None:
            net_sales_value = gross_value - refund_value
            net_sales_reason = None
        else:
            # refund 缺失：可能是"无退款"（净额=gross）或"退款无法归属"（净额 MISSING）。
            # 只有 multi_line 判定为"无法归属"时才 MISSING；无退款记录但单行订单 → 净额=gross。
            if multi_line is not None:
                net_sales_value = None
                net_sales_reason = "net_sales_not_attributable_to_sku_multi_line_order"
            else:
                net_sales_value = gross_value
                net_sales_reason = None
        return {
            "payments": _to_float(row["order_qty"]),
            "refunds": refund_value,
            "net_sales": net_sales_value,
            # data_as_of 统一源摄入时间（证据审查 #4：与库存口径一致，
            # 补录旧订单不低估新鲜度）；业务窗口过滤仍用 placed_at
            "data_as_of": datetime.fromisoformat(row["latest_updated"])
            if row["latest_updated"] else None,
            # 来源诚实（证据审查 #1）：取最新行的真实 source_id，不合成前缀串
            "source_ref": row["latest_source_id"],
            "authoritative_service": "commerce_orders",
            "connector_id": row["connector_id"],
            # 字段级 reason（三路独立）：refund 缺失只作用于 refunds，
            # net_sales 无法归属只作用于 net_sales；payments 有值时 reason 应为 None。
            "reason": None,
            "refund_reason": refund_reason,
            "net_sales_reason": net_sales_reason,
            # A8（盲点 #3 修复）：订单域是 revision 窗口聚合（payments/refunds/net_sales
            # SUM 整个窗口），粒度必须标 WINDOW 而非默认 DAILY——避免消费方误当日粒度
            # 与"窗口 SUM 标 DAILY"式静默混粒度（任务书 L141，模块头注释自禁）。
            "granularity": Granularity.WINDOW,
            "period_key": (window["active_from"] or "")[:10] or None,
        }

    def _missing_orders(self, reason: str) -> dict[str, Any]:
        return {
            "payments": None, "refunds": None, "net_sales": None,
            "data_as_of": None, "source_ref": None, "connector_id": None,
            "granularity": None,
            "authoritative_service": "commerce_orders",
            "reason": reason,
            "refund_reason": reason,  # 订单缺失时退款同样缺失，共用同一 reason
            "net_sales_reason": reason,  # 订单缺失时 net_sales 同样缺失，共用同一 reason
            "period_key": None,
        }

    @staticmethod
    def _facts_for(
        field: str,
        traffic: dict[str, Any],
        inventory: dict[str, Any],
        orders: dict[str, Any],
    ) -> dict[str, Any]:
        """按字段从对应事实源取投影（确定性）。返回统一 shape 含 value。

        reason 字段级（三路独立，互不污染）：
        - refunds 用 refund_reason（退款缺失的独立 reason）；
        - net_sales 用 net_sales_reason（无法归属多行订单的独立 reason）；
        - 其余字段（payments/orders）用通用 reason（订单缺失的 reason，有值时 None）。
        """
        if field in ("impressions", "clicks", "add_to_cart", "orders"):
            source = traffic
        elif field in ("sellable_stock", "in_transit_stock"):
            source = inventory
        else:
            source = orders
        # 源 dict 用字段名存值；统一补 value 键给投影用
        result = dict(source)
        result["value"] = source.get(field)
        # 字段级 reason：refunds 用 refund_reason，net_sales 用 net_sales_reason，
        # 其余字段用通用 reason（有值时 None）
        if field == "refunds":
            result["reason"] = source.get("refund_reason")
        elif field == "net_sales":
            result["reason"] = source.get("net_sales_reason")
        return result

    # ── 商品域（真实查询：readonly_product_mapping_events + readonly_canonical_products）──

    def _product_mapping(
        self, tenant_id: str, store_id: str, item_id: str, sku_id: str
    ) -> dict[str, Any] | None:
        """SKU → canonical 映射（按权威 connector + item 过滤；revoked 使映射失效）。

        R2（证据诚实）：
        - 同 SKU 不同 item 可能有各自映射，必须带 item_id 过滤（不跨 item 串料号）。
        - mapping_version 是每 (tenant,store,connector,sku) 独立序列，必须按权威
          connector 过滤（否则跨 connector 取最大 version 会用 demo 高 version 掩盖
          operational 的 revoked）。权威 connector = 该 SKU listing_revisions 最新行
          （对齐 M7-R get_latest_mapping 的 connector 作用域）。
        - revoked 语义：取最新事件（不过滤 event_type），最新事件是 revoked → 无映射。
        """
        with self.db.connect() as conn:
            # 权威 connector：该 SKU 在 listing_revisions 的"最新"revision 所属 connector。
            # ⚠️ 不能按 revision_no DESC 选——revision_no 是每 (tenant,connector,store,item,sku)
            # 独立序列（database.py UNIQUE 含 connector_id），跨 connector 数值不可比
            # （demo 高 revision_no 会掩盖 operational 低 revision_no）。必须按跨 connector
            # 可比的真实时间 source_updated_at（NULL 排最后），加 id 尾键确定性。
            rev_row = conn.execute(
                """
                SELECT connector_id FROM listing_revisions
                WHERE tenant_id=? AND store_id=? AND item_id=? AND sku_id=?
                  AND connector_id IS NOT NULL
                -- A5（盲点 #14 修复）：同 source_updated_at 平局时不用 revision_no 跨
                -- connector 比较（每 connector 独立序列不可比），用 id 尾键确定
                ORDER BY source_updated_at DESC, id DESC
                LIMIT 1
                """,
                (tenant_id, store_id, item_id, sku_id),
            ).fetchone()
            connector_id = rev_row["connector_id"] if rev_row else None
            if connector_id is None:
                return None
            row = conn.execute(
                """
                -- 负责人复验阻断项 4：先解析该 SKU 流的最新事件（不过滤 item），
                -- 再判断它是否仍归属所查询 item。修复前先用 item 过滤再排序——
                -- 最新事件已到 item-b 时查询 item-a 会回落到 item-a 的旧 v1（映射复活）。
                -- 最新事件是 v2=item-b → item-a 不再是最新归属 → 返回 None，不复活旧映射。
                SELECT m.event_id, m.event_type, m.mapping_version,
                       m.canonical_product_id, m.item_id, m.merchant_code,
                       m.created_at
                FROM readonly_product_mapping_events m
                WHERE m.tenant_id=? AND m.store_id=? AND m.connector_id=?
                  AND m.sku_id=?
                ORDER BY m.mapping_version DESC, m.event_id DESC
                LIMIT 1
                """,
                (tenant_id, store_id, connector_id, sku_id),
            ).fetchone()
        # 最新事件不在查询 item（NULL 共享事件视为非专属归属）→ 不复活旧映射
        if row is None or row["event_type"] == "revoked":
            return None
        if row["item_id"] not in (item_id, None):
            return None
        return {
            "canonical_product_id": row["canonical_product_id"],
            "item_id": row["item_id"],
            "merchant_code": row["merchant_code"],
            "connector_id": connector_id,
            "event_id": row["event_id"],
            "mapping_version": row["mapping_version"],
            "created_at": row["created_at"],
        }

    def _product_identity(
        self, tenant_id: str, store_id: str, item_id: str, sku_id: str
    ) -> dict[str, Any] | None:
        """Consume the latest operational M7-R matched reconciliation row.

        A confirmed mapping alone is intentionally insufficient. The M7-R handoff
        requires downstream modules to consume the immutable matched row and keep
        its run, policy and mapping snapshot references. A newer ambiguous,
        unmapped or rejected reconciliation therefore suppresses the material code.
        """
        mapping = self._product_mapping(tenant_id, store_id, item_id, sku_id)
        if mapping is None:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """
                WITH latest_run AS (
                    SELECT run_id, policy_version, mapping_snapshot_digest, created_at
                    FROM readonly_product_reconciliation_runs
                    WHERE tenant_id=? AND store_id=? AND data_scope='operational'
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT 1
                )
                SELECT rr.row_id, rr.run_id, rr.canonical_product_id,
                       rr.internal_part_number, rr.connector_id, rr.source_domain,
                       rr.source_reference, lr.policy_version,
                       lr.mapping_snapshot_digest, lr.created_at,
                       p.title, p.merchant_code
                FROM latest_run lr
                JOIN readonly_product_reconciliation_rows rr
                  ON rr.tenant_id=? AND rr.store_id=? AND rr.run_id=lr.run_id
                JOIN readonly_canonical_products p
                  ON p.tenant_id=rr.tenant_id AND p.store_id=rr.store_id
                 AND p.canonical_product_id=rr.canonical_product_id
                WHERE rr.terminal_status='matched'
                  AND rr.connector_id=? AND rr.sku_id=?
                  AND (rr.item_id=? OR rr.item_id IS NULL)
                  AND rr.canonical_product_id=?
                  AND lr.created_at>=?
                ORDER BY CASE WHEN rr.item_id=? THEN 0 ELSE 1 END,
                         rr.row_number, rr.row_id
                LIMIT 1
                """,
                (
                    tenant_id, store_id, tenant_id, store_id,
                    mapping["connector_id"], sku_id, item_id,
                    mapping["canonical_product_id"], mapping["created_at"], item_id,
                ),
            ).fetchone()
        if row is None:
            return None
        evidence = ProductIdentityEvidence(
            canonical_product_id=str(row["canonical_product_id"]),
            internal_part_number=str(row["internal_part_number"]),
            run_id=str(row["run_id"]),
            row_id=str(row["row_id"]),
            policy_version=str(row["policy_version"]),
            mapping_snapshot_digest=str(row["mapping_snapshot_digest"]),
            connector_id=str(row["connector_id"]),
            source_domain=str(row["source_domain"]),
            source_reference=(
                str(row["source_reference"])
                if row["source_reference"] is not None else None
            ),
            reconciled_at=datetime.fromisoformat(str(row["created_at"])),
        )
        return {
            "material_code": evidence.internal_part_number,
            "title": str(row["title"]),
            "merchant_code": (
                str(row["merchant_code"])
                if row["merchant_code"] is not None else None
            ),
            "evidence": evidence,
        }

    # ── 竞品域（真实查询：competitor_observations）──

    def _competitor_price(
        self, tenant_id: str, store_id: str, sku_id: str
    ) -> MetricValue:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT o.competitor_price, o.observed_at, o.source_id,
                       o.connector_id
                FROM competitor_observations o
                JOIN competitive_entity_matches m
                  ON m.id=o.entity_match_id
                 AND m.tenant_id=o.tenant_id
                 AND m.connector_id=o.connector_id
                 AND m.store_id=o.store_id
                 AND m.subject_sku=o.subject_sku
                 AND m.competitor_name=o.competitor_name
                 AND m.competitor_sku=o.competitor_sku
                WHERE o.tenant_id=? AND o.store_id=? AND o.subject_sku=?
                  AND m.status='approved'
                -- A6（盲点 #15 修复）：同 observed_at 多行（多竞品/多 connector）时
                -- 加唯一尾键确定（connector_id + competitor_sku + id），避免任取
                ORDER BY o.observed_at DESC, o.connector_id,
                         o.competitor_sku, o.id DESC
                LIMIT 1
                """,
                (tenant_id, store_id, sku_id),
            ).fetchone()
        if row is None:
            return MetricValue.missing(
                Granularity.DAILY, AggregateRule.NONE, _PERIOD_PLACEHOLDER,
                reason="competitor_approved_evidence_not_found",
            )
        source_type = source_type_from_connector(row["connector_id"])
        state = evidence_state_from_source_type(source_type)
        if state is EvidenceState.MISSING:
            return MetricValue.missing(
                Granularity.DAILY, AggregateRule.NONE, _PERIOD_PLACEHOLDER,
                reason="competitor_source_unknown",
            )
        return MetricValue.from_value(
            state=state,
            granularity=Granularity.DAILY,
            aggregate_rule=AggregateRule.NONE,
            period_key=(row["observed_at"] or "")[:10] or _PERIOD_PLACEHOLDER,
            value=_to_float(row["competitor_price"]),
            import_manifest_id=row["source_id"],
            data_as_of=(
                datetime.fromisoformat(row["observed_at"])
                if row["observed_at"] else None
            ),
            authoritative_service="competitor_observations",
            data_trust=(
                DataTrust.DEMO if state is EvidenceState.DEMO
                else DataTrust.PRODUCTION
            ),
        )

    # ── 广告/实验域（SKU 级缺来源 → 显式 MISSING）──

    @staticmethod
    def _ad_spend_missing() -> MetricValue:
        return MetricValue.missing(
            Granularity.DAILY, AggregateRule.SUM, _PERIOD_PLACEHOLDER,
            reason="ad_metric_store_level_only",
        )

    @staticmethod
    def _experiment_missing() -> MetricValue:
        return MetricValue.missing(
            Granularity.DAILY, AggregateRule.NONE, _PERIOD_PLACEHOLDER,
            reason="experiment_state_provided_by_wp2_bridge",
        )


__all__ = [
    "ProductReadQuery",
]
