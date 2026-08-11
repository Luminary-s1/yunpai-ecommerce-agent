from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..database import Database, utc_now
from .models import DEMAND_V1, DemandFactRebuild

if TYPE_CHECKING:
    from ..business.inventory import InventoryService
    from ..business.orders import OrderService


_POLICY_TIMEZONE = ZoneInfo(DEMAND_V1.timezone)
_CENT = Decimal("0.01")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _decimal(value: str | int | Decimal) -> Decimal:
    return Decimal(str(value))


def _money(value: Decimal) -> str:
    return format(value.quantize(_CENT, rounding=ROUND_HALF_UP), "f")


class DemandFactService:
    """Build immutable demand-v1 facts from public order and inventory services."""

    def __init__(
        self,
        db: Database,
        *,
        orders: OrderService,
        inventory: InventoryService,
    ):
        self.db = db
        self.orders = orders
        self.inventory = inventory

    def rebuild(self, tenant_id: str, request: DemandFactRebuild) -> dict[str, Any]:
        current_date = datetime.now(_POLICY_TIMEZONE).date()
        start_date, end_date = request.resolved_window(current_business_date=current_date)
        start_at = self._start_of_business_day(start_date)
        end_at = self._start_of_business_day(end_date + timedelta(days=1))
        source_orders = self.orders.demand_source_orders(
            tenant_id,
            store_id=request.store_id,
            start_at=start_at,
            end_at=end_at,
        )
        balances = self.inventory.list_balances(tenant_id, store_id=request.store_id)
        grouped = self._group_order_lines(
            source_orders,
            start_date=start_date,
            end_date=end_date,
            sku_id=request.sku_id,
        )
        sku_ids = {sku for _business_date, sku in grouped}
        if request.sku_id is not None:
            sku_ids.add(request.sku_id)
        records = [
            self._fact_record(
                tenant_id=tenant_id,
                store_id=request.store_id,
                sku_id=sku_id,
                business_date=business_date,
                entries=grouped[(business_date, sku_id)],
                balances=balances,
                coverage_complete=request.coverage_complete,
            )
            for business_date in self._date_range(start_date, end_date)
            for sku_id in sorted(sku_ids)
        ]
        written = 0
        idempotent = 0
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in records:
                if self._append_if_changed(conn, record):
                    written += 1
                else:
                    idempotent += 1
        return {
            "mode": request.mode,
            "store_id": request.store_id,
            "sku_id": request.sku_id,
            "window": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "coverage_complete": request.coverage_complete,
            "demand_policy": DEMAND_V1.evidence(),
            "facts_written": written,
            "facts_idempotent": idempotent,
        }

    def list_facts(
        self,
        tenant_id: str,
        *,
        store_id: str,
        sku_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        conditions = ["f.tenant_id=?", "f.store_id=?"]
        params: list[Any] = [tenant_id, store_id]
        if sku_id is not None:
            conditions.append("f.sku_id=?")
            params.append(sku_id)
        if start_date is not None:
            conditions.append("f.business_date>=?")
            params.append(start_date.isoformat())
        if end_date is not None:
            conditions.append("f.business_date<=?")
            params.append(end_date.isoformat())
        if not include_history:
            conditions.append(
                """NOT EXISTS (
                    SELECT 1 FROM demand_daily_facts newer
                    WHERE newer.tenant_id=f.tenant_id
                      AND newer.store_id=f.store_id
                      AND newer.sku_id=f.sku_id
                      AND newer.business_date=f.business_date
                      AND newer.demand_policy_version=f.demand_policy_version
                      AND newer.fact_version>f.fact_version
                )"""
            )
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT f.* FROM demand_daily_facts f
                WHERE {' AND '.join(conditions)}
                ORDER BY f.business_date, f.sku_id, f.fact_version
                """,
                tuple(params),
            ).fetchall()
        return [self._view(dict(row)) for row in rows]

    @staticmethod
    def _start_of_business_day(value: date) -> datetime:
        return datetime.combine(value, time.min, tzinfo=_POLICY_TIMEZONE).astimezone(UTC)

    @staticmethod
    def _date_range(start_date: date, end_date: date) -> list[date]:
        days = (end_date - start_date).days
        return [start_date + timedelta(days=offset) for offset in range(days + 1)]

    @staticmethod
    def _group_order_lines(
        orders: list[dict[str, Any]],
        *,
        start_date: date,
        end_date: date,
        sku_id: str | None,
    ) -> dict[tuple[date, str], list[tuple[dict[str, Any], dict[str, Any]]]]:
        grouped: dict[tuple[date, str], list[tuple[dict[str, Any], dict[str, Any]]]] = (
            defaultdict(list)
        )
        for order in orders:
            business_date = _time(str(order["placed_at"])).astimezone(_POLICY_TIMEZONE).date()
            if business_date < start_date or business_date > end_date:
                continue
            for line in order["lines"]:
                if sku_id is None or line["sku_id"] == sku_id:
                    grouped[(business_date, str(line["sku_id"]))].append((order, line))
        return grouped

    def _fact_record(
        self,
        *,
        tenant_id: str,
        store_id: str,
        sku_id: str,
        business_date: date,
        entries: list[tuple[dict[str, Any], dict[str, Any]]],
        balances: list[dict[str, Any]],
        coverage_complete: bool,
    ) -> dict[str, Any]:
        from ..business.source_versioning import payload_digest

        stockout_flag, available_stock, stockout_evidence, inventory_lineage = (
            self._stockout_evidence(balances, sku_id=sku_id, business_date=business_date)
        )
        gross_units, eligible_units, order_count, sales_amount, price, quality_flags = (
            self._demand_values(entries, coverage_complete=coverage_complete)
        )
        if stockout_flag == "unknown":
            quality_flags.add("stockout_unknown")
        order_lineage = self._order_lineage(entries)
        source_watermark = {
            "orders": self._watermark(order_lineage),
            "inventory": self._watermark(inventory_lineage),
        }
        lineage = {"orders": order_lineage, "inventory": inventory_lineage}
        payload = {
            "tenant_id": tenant_id,
            "store_id": store_id,
            "sku_id": sku_id,
            "business_date": business_date.isoformat(),
            "gross_units": gross_units,
            "eligible_units": eligible_units,
            "order_count": order_count,
            "sales_amount": sales_amount,
            "available_stock": available_stock,
            "stockout_flag": stockout_flag,
            "stockout_evidence": stockout_evidence,
            "price": price,
            "promotion_flag": "unknown",
            "source_watermark": source_watermark,
            "demand_policy_version": DEMAND_V1.policy_version,
            "quality_flags": sorted(quality_flags),
            "lineage": lineage,
        }
        return {**payload, "payload_hash": payload_digest(payload)}

    @staticmethod
    def _demand_values(
        entries: list[tuple[dict[str, Any], dict[str, Any]]], *, coverage_complete: bool
    ) -> tuple[int | None, int | None, int | None, str | None, str | None, set[str]]:
        quality_flags: set[str] = set()
        if not entries:
            if coverage_complete:
                return 0, 0, 0, "0.00", None, {"zero_demand"}
            return None, None, None, None, None, {"data_coverage_missing"}
        gross_units = sum(int(line["quantity"]) for _order, line in entries)
        eligible_entries = [
            (order, line)
            for order, line in entries
            if (
                order["payment_status"] in DEMAND_V1.included_payment_statuses
                and order["order_status"] not in DEMAND_V1.excluded_order_statuses
            )
        ]
        eligible_units = sum(int(line["quantity"]) for _order, line in eligible_entries)
        eligible_order_ids = {str(order["id"]) for order, _line in eligible_entries}
        sales = sum(
            (_decimal(line["unit_price"]) * int(line["quantity"]) for _order, line in eligible_entries),
            Decimal("0"),
        )
        if not coverage_complete:
            quality_flags.add("source_coverage_unconfirmed")
        if eligible_units == 0:
            quality_flags.add("no_eligible_demand")
        return (
            gross_units,
            eligible_units,
            len(eligible_order_ids),
            _money(sales),
            _money(sales / eligible_units) if eligible_units else None,
            quality_flags,
        )

    def _stockout_evidence(
        self,
        balances: list[dict[str, Any]],
        *,
        sku_id: str,
        business_date: date,
    ) -> tuple[str, str | None, dict[str, Any], list[dict[str, Any]]]:
        relevant = [
            item
            for item in balances
            if (
                item["sku_id"] == sku_id
                and _time(str(item["source_updated_at"])).astimezone(_POLICY_TIMEZONE).date()
                == business_date
            )
        ]
        lineage = [
            {
                "balance_id": item["id"],
                "warehouse_id": item["warehouse_id"],
                "source_id": item["source_id"],
                "source_updated_at": item["source_updated_at"],
                "version": item["version"],
            }
            for item in sorted(relevant, key=lambda value: (value["warehouse_id"], value["id"]))
        ]
        if not relevant:
            return (
                "unknown",
                None,
                {
                    "reason": "no_same_business_date_inventory_snapshot",
                    "timezone": DEMAND_V1.timezone,
                },
                lineage,
            )
        warehouses = [str(item["warehouse_id"]) for item in relevant]
        if len(warehouses) != len(set(warehouses)):
            return (
                "unknown",
                None,
                {"reason": "ambiguous_warehouse_inventory_snapshot"},
                lineage,
            )
        available = sum(
            (max(Decimal("0"), _decimal(item["on_hand"]) - _decimal(item["reserved"])) for item in relevant),
            Decimal("0"),
        )
        available_text = _money(available)
        return (
            "true" if available <= 0 else "false",
            available_text,
            {"available_stock": available_text, "snapshot_count": len(relevant)},
            lineage,
        )

    @staticmethod
    def _order_lineage(
        entries: list[tuple[dict[str, Any], dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        by_order = {
            str(order["id"]): {
                "order_id": order["order_id"],
                "source_id": order["source_id"],
                "source_updated_at": order["source_updated_at"],
                "version": order["version"],
            }
            for order, _line in entries
        }
        return [by_order[key] for key in sorted(by_order)]

    @staticmethod
    def _watermark(lineage: list[dict[str, Any]]) -> dict[str, Any]:
        timestamps = [str(item["source_updated_at"]) for item in lineage]
        return {
            "count": len(lineage),
            "max_source_updated_at": max(timestamps) if timestamps else None,
        }

    def _append_if_changed(self, conn: Any, record: dict[str, Any]) -> bool:
        existing = conn.execute(
            """
            SELECT fact_version, payload_hash FROM demand_daily_facts
            WHERE tenant_id=? AND store_id=? AND sku_id=? AND business_date=?
              AND demand_policy_version=?
            ORDER BY fact_version DESC LIMIT 1
            """,
            (
                record["tenant_id"],
                record["store_id"],
                record["sku_id"],
                record["business_date"],
                record["demand_policy_version"],
            ),
        ).fetchone()
        if existing is not None and str(existing["payload_hash"]) == record["payload_hash"]:
            return False
        version = int(existing["fact_version"]) + 1 if existing is not None else 1
        conn.execute(
            """
            INSERT INTO demand_daily_facts(
                id, tenant_id, store_id, sku_id, business_date, gross_units,
                eligible_units, order_count, sales_amount, available_stock,
                stockout_flag, stockout_evidence_json, price, promotion_flag,
                source_watermark, fact_version, demand_policy_version,
                quality_flags_json, lineage_json, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"demand-fact-{uuid.uuid4().hex}",
                record["tenant_id"],
                record["store_id"],
                record["sku_id"],
                record["business_date"],
                record["gross_units"],
                record["eligible_units"],
                record["order_count"],
                record["sales_amount"],
                record["available_stock"],
                record["stockout_flag"],
                _json(record["stockout_evidence"]),
                record["price"],
                record["promotion_flag"],
                _json(record["source_watermark"]),
                version,
                record["demand_policy_version"],
                _json(record["quality_flags"]),
                _json(record["lineage"]),
                record["payload_hash"],
                utc_now(),
            ),
        )
        return True

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **{
                key: row[key]
                for key in (
                    "id",
                    "tenant_id",
                    "store_id",
                    "sku_id",
                    "business_date",
                    "gross_units",
                    "eligible_units",
                    "order_count",
                    "sales_amount",
                    "available_stock",
                    "stockout_flag",
                    "price",
                    "promotion_flag",
                    "fact_version",
                    "demand_policy_version",
                    "payload_hash",
                    "created_at",
                )
            },
            "stockout_evidence": json.loads(row["stockout_evidence_json"]),
            "source_watermark": json.loads(row["source_watermark"]),
            "quality_flags": json.loads(row["quality_flags_json"]),
            "lineage": json.loads(row["lineage_json"]),
        }
