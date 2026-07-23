from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..database import Database, utc_now
from .inventory import InventoryService


MetricName = Literal[
    "active_sku_count",
    "order_count",
    "gross_revenue",
    "after_sale_order_rate",
    "inventory_risk_count",
    "competitor_lower_price_count",
]


class MetricQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: MetricName
    store_id: str | None = Field(default=None, max_length=128)
    sku_id: str | None = Field(default=None, max_length=128)


class MetricsService:
    DEFINITION_VERSION = "1.0"
    DEFINITIONS = {
        "active_sku_count": ("在售 SKU 数", "count"),
        "order_count": ("订单数（不含已取消）", "count"),
        "gross_revenue": ("已支付且未取消订单金额", "currency"),
        "after_sale_order_rate": ("存在售后单的订单占比", "ratio"),
        "inventory_risk_count": ("高危或严重库存风险数", "count"),
        "competitor_lower_price_count": ("竞品价格低于本品的观察数", "count"),
    }

    def __init__(self, db: Database, inventory: InventoryService):
        self.db = db
        self.inventory = inventory

    def catalog(self) -> list[dict[str, str]]:
        return [
            {
                "metric": key,
                "display_name": value[0],
                "unit": value[1],
                "definition_version": self.DEFINITION_VERSION,
            }
            for key, value in self.DEFINITIONS.items()
        ]

    def query(self, tenant_id: str, query: MetricQuery) -> dict[str, Any]:
        handlers = {
            "active_sku_count": self._active_sku_count,
            "order_count": self._order_count,
            "gross_revenue": self._gross_revenue,
            "after_sale_order_rate": self._after_sale_order_rate,
            "inventory_risk_count": self._inventory_risk_count,
            "competitor_lower_price_count": self._competitor_lower_price_count,
        }
        value, data_as_of, evidence_count = handlers[query.metric](tenant_id, query)
        unit = self.DEFINITIONS[query.metric][1]
        return {
            "metric": query.metric,
            "display_name": self.DEFINITIONS[query.metric][0],
            "value": value,
            "unit": unit,
            "definition_version": self.DEFINITION_VERSION,
            "filters": query.model_dump(exclude={"metric"}, exclude_none=True),
            "data_as_of": data_as_of,
            "quality": "available" if evidence_count else "no_data",
            "evidence_count": evidence_count,
            "computed_at": utc_now(),
        }

    @staticmethod
    def _filters(query: MetricQuery, *, sku_column: str | None = None) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.store_id:
            clauses.append("store_id=?")
            params.append(query.store_id)
        if query.sku_id and sku_column:
            clauses.append(f"{sku_column}=?")
            params.append(query.sku_id)
        return clauses, params

    def _active_sku_count(self, tenant_id: str, query: MetricQuery) -> tuple[int, str | None, int]:
        clauses, params = self._filters(query, sku_column="sku_id")
        clauses = ["tenant_id=?", "status='active'", *clauses]
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) count, MAX(source_updated_at) data_as_of FROM catalog_items WHERE {' AND '.join(clauses)}",
                (tenant_id, *params),
            ).fetchone()
        count = int(row["count"])
        return count, row["data_as_of"], count

    def _order_rows(self, tenant_id: str, query: MetricQuery) -> list[Any]:
        clauses, params = self._filters(query)
        clauses = ["o.tenant_id=?", "o.order_status!='canceled'", *[f"o.{c}" for c in clauses]]
        join = ""
        if query.sku_id:
            join = " JOIN commerce_order_lines l ON l.order_id=o.id"
            clauses.append("l.sku_id=?")
            params.append(query.sku_id)
        with self.db.connect() as conn:
            return conn.execute(
                f"SELECT DISTINCT o.* FROM commerce_orders o{join} WHERE {' AND '.join(clauses)}",
                (tenant_id, *params),
            ).fetchall()

    def _order_count(self, tenant_id: str, query: MetricQuery) -> tuple[int, str | None, int]:
        rows = self._order_rows(tenant_id, query)
        data_as_of = max((str(row["source_updated_at"]) for row in rows), default=None)
        return len(rows), data_as_of, len(rows)

    def _gross_revenue(self, tenant_id: str, query: MetricQuery) -> tuple[str, str | None, int]:
        rows = [row for row in self._order_rows(tenant_id, query) if row["payment_status"] in {"paid", "partially_refunded"}]
        value = sum((Decimal(str(row["total_amount"])) for row in rows), Decimal("0"))
        data_as_of = max((str(row["source_updated_at"]) for row in rows), default=None)
        return format(value.quantize(Decimal("0.01")), "f"), data_as_of, len(rows)

    def _after_sale_order_rate(self, tenant_id: str, query: MetricQuery) -> tuple[str, str | None, int]:
        rows = self._order_rows(tenant_id, query)
        if not rows:
            return "0.0000", None, 0
        ids = [str(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        with self.db.connect() as conn:
            count = int(conn.execute(
                f"SELECT COUNT(DISTINCT order_id) FROM commerce_after_sale_cases WHERE order_id IN ({placeholders})",
                tuple(ids),
            ).fetchone()[0])
        ratio = Decimal(count) / Decimal(len(rows))
        data_as_of = max(str(row["source_updated_at"]) for row in rows)
        return format(ratio.quantize(Decimal("0.0001")), "f"), data_as_of, len(rows)

    def _inventory_risk_count(self, tenant_id: str, query: MetricQuery) -> tuple[int, str | None, int]:
        risks = self.inventory.risks(tenant_id, store_id=query.store_id, sku_id=query.sku_id)
        risky = [item for item in risks if item["risk_level"] in {"high", "critical"}]
        data_as_of = max((str(item["evidence"]["data_as_of"]) for item in risks), default=None)
        return len(risky), data_as_of, len(risks)

    def _competitor_lower_price_count(self, tenant_id: str, query: MetricQuery) -> tuple[int, str | None, int]:
        clauses, params = self._filters(query, sku_column="subject_sku")
        clauses = ["tenant_id=?", "CAST(competitor_price AS REAL) < CAST(subject_price AS REAL)", *clauses]
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) count, MAX(observed_at) data_as_of FROM competitor_observations WHERE {' AND '.join(clauses)}",
                (tenant_id, *params),
            ).fetchone()
        count = int(row["count"])
        return count, row["data_as_of"], count
