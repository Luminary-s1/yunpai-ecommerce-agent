from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..database import Database, utc_now
from .source_versioning import canonical_source_time, decide_write, payload_digest


class InventoryBalanceUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    warehouse_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    item_id: str | None = Field(default=None, max_length=128)  # P1: 链接展示维度（SKU 共享数据留 NULL）
    on_hand: Decimal = Field(ge=0)
    reserved: Decimal = Field(default=Decimal("0"), ge=0)
    inbound: Decimal = Field(default=Decimal("0"), ge=0)
    average_daily_sales: Decimal = Field(default=Decimal("0"), ge=0)
    source_updated_at: datetime
    source_id: str | None = Field(default=None, max_length=256)

    @field_validator("source_updated_at")
    @classmethod
    def require_aware_source_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value


class InventoryService:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, tenant_id: str, value: InventoryBalanceUpsert) -> dict[str, Any]:
        payload = value.model_dump(mode="json")
        source_time = canonical_source_time(value.source_updated_at)
        payload["source_updated_at"] = source_time
        payload_hash = payload_digest(payload)
        now = utc_now()
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT id, version, source_updated_at, payload_hash FROM inventory_balances
                WHERE tenant_id=? AND connector_id=? AND store_id=?
                  AND warehouse_id=? AND sku_id=?
                """,
                (
                    tenant_id,
                    value.connector_id,
                    value.store_id,
                    value.warehouse_id,
                    value.sku_id,
                ),
            ).fetchone()
            if existing is not None:
                decision = decide_write(
                    existing_source_time=str(existing["source_updated_at"]),
                    existing_payload_hash=str(existing["payload_hash"]),
                    incoming_source_time=source_time,
                    incoming_payload_hash=payload_hash,
                )
                if decision == "idempotent":
                    write_status = "idempotent"
            balance_id = str(existing["id"]) if existing else f"inventory-{uuid.uuid4().hex}"
            if write_status == "applied":
                version = int(existing["version"]) + 1 if existing else 1
                conn.execute(
                """
                INSERT INTO inventory_balances(
                    id, tenant_id, connector_id, store_id, warehouse_id, sku_id,
                    item_id, on_hand, reserved, inbound, average_daily_sales, source_id,
                    source_updated_at, payload_hash, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, connector_id, store_id, warehouse_id, sku_id)
                DO UPDATE SET
                    on_hand=excluded.on_hand, reserved=excluded.reserved,
                    inbound=excluded.inbound,
                    average_daily_sales=excluded.average_daily_sales,
                    source_id=excluded.source_id,
                    source_updated_at=excluded.source_updated_at,
                    payload_hash=excluded.payload_hash,
                    version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                    (
                    balance_id,
                    tenant_id,
                    value.connector_id,
                    value.store_id,
                    value.warehouse_id,
                    value.sku_id,
                    value.item_id,
                    str(value.on_hand),
                    str(value.reserved),
                    str(value.inbound),
                    str(value.average_daily_sales),
                    value.source_id,
                    source_time,
                    payload_hash,
                    version,
                    now,
                    now,
                    ),
                )
        result = self._row_by_id(balance_id)
        result["write_status"] = write_status
        return result

    def list_balances(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        sku_id: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if sku_id:
            conditions.append("sku_id=?")
            params.append(sku_id)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM inventory_balances
                WHERE {' AND '.join(conditions)}
                ORDER BY store_id, warehouse_id, sku_id
                """,
                tuple(params),
            ).fetchall()
        return [self._view(dict(row)) for row in rows]

    def risks(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        sku_id: str | None = None,
        reorder_lead_days: int = 7,
        target_days: int = 30,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for row in self.list_balances(tenant_id, store_id=store_id, sku_id=sku_id):
            on_hand = Decimal(row["on_hand"])
            reserved = Decimal(row["reserved"])
            inbound = Decimal(row["inbound"])
            velocity = Decimal(row["average_daily_sales"])
            available = max(Decimal("0"), on_hand - reserved)
            coverage = available / velocity if velocity > 0 else None
            reorder = max(
                Decimal("0"),
                Decimal(target_days) * velocity - available - inbound,
            )
            if available <= 0:
                risk_code, risk_level = "stockout", "critical"
            elif velocity == 0:
                risk_code, risk_level = "slow_moving", "medium"
            elif coverage is not None and coverage <= reorder_lead_days:
                risk_code, risk_level = "stockout_risk", "high"
            elif coverage is not None and coverage < target_days:
                risk_code, risk_level = "replenishment_due", "medium"
            else:
                risk_code, risk_level = "healthy", "low"
            results.append(
                {
                    "balance_id": row["id"],
                    "store_id": row["store_id"],
                    "warehouse_id": row["warehouse_id"],
                    "sku_id": row["sku_id"],
                    "risk_code": risk_code,
                    "risk_level": risk_level,
                    "available": self._decimal(available),
                    "coverage_days": self._decimal(coverage) if coverage is not None else None,
                    "recommended_replenishment": self._decimal(reorder),
                    "assumptions": {
                        "reorder_lead_days": reorder_lead_days,
                        "target_days": target_days,
                        "average_daily_sales": row["average_daily_sales"],
                    },
                    "evidence": {
                        "connector_id": row["connector_id"],
                        "source_id": row["source_id"],
                        "data_as_of": row["source_updated_at"],
                        "version": row["version"],
                    },
                }
            )
        return results

    def _row_by_id(self, balance_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM inventory_balances WHERE id=?", (balance_id,)
            ).fetchone()
        if row is None:
            raise ValueError("inventory balance not found")
        return self._view(dict(row))

    @staticmethod
    def _decimal(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "id",
                "connector_id",
                "store_id",
                "warehouse_id",
                "sku_id",
                "on_hand",
                "reserved",
                "inbound",
                "average_daily_sales",
                "source_id",
                "source_updated_at",
                "version",
                "updated_at",
            )
        }
