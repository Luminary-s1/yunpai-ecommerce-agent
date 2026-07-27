from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..database import Database, session_scope_condition, utc_now
from .source_versioning import canonical_source_time, decide_write, payload_digest


OrderStatus = Literal[
    "created", "paid", "fulfilling", "shipped", "delivered", "closed", "canceled"
]
PaymentStatus = Literal["unpaid", "paid", "partially_refunded", "refunded", "closed"]
LogisticsStatus = Literal["pending", "collected", "in_transit", "delivered", "exception"]
AfterSaleStatus = Literal[
    "requested", "reviewing", "approved", "rejected", "returning", "completed", "canceled"
]

CUSTOMER_SERVICE_STATUS = {
    "proposed": ("waiting", "等待客服"),
    "accepted": ("assigned", "客服已接单"),
    "working": ("processing", "客服处理中"),
    "input_required": ("waiting_input", "等待客户补充"),
    "review": ("processing", "客服处理中"),
}


class OrderLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    quantity: int = Field(ge=1, le=100000)
    unit_price: Decimal = Field(ge=0)


class LogisticsSnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    carrier: str = Field(min_length=1, max_length=128)
    tracking_no_masked: str = Field(min_length=3, max_length=128)
    status: LogisticsStatus
    last_event: str = Field(min_length=1, max_length=500)
    last_event_at: datetime

    @field_validator("last_event_at")
    @classmethod
    def require_aware_event_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value


class AfterSaleCaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=128)
    case_type: Literal["refund", "return_refund", "exchange", "repair", "complaint"]
    status: AfterSaleStatus
    requested_amount: Decimal = Field(default=Decimal("0"), ge=0)
    approved_amount: Decimal = Field(default=Decimal("0"), ge=0)
    reason_code: str | None = Field(default=None, max_length=128)
    opened_at: datetime
    updated_at: datetime

    @field_validator("opened_at", "updated_at")
    @classmethod
    def require_aware_case_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value


class OrderUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    order_id: str = Field(min_length=1, max_length=128)
    order_status: OrderStatus
    payment_status: PaymentStatus
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    total_amount: Decimal = Field(ge=0)
    placed_at: datetime
    buyer_ref_hash: str | None = Field(default=None, min_length=16, max_length=128)
    lines: list[OrderLineInput] = Field(min_length=1, max_length=500)
    logistics: LogisticsSnapshotInput | None = None
    after_sales: list[AfterSaleCaseInput] = Field(default_factory=list, max_length=100)
    source_updated_at: datetime
    source_id: str | None = Field(default=None, max_length=256)

    @field_validator("placed_at", "source_updated_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value

    @model_validator(mode="after")
    def unique_child_ids(self) -> "OrderUpsert":
        line_ids = [line.line_id for line in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("duplicate_order_line_id")
        case_ids = [case.case_id for case in self.after_sales]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("duplicate_after_sale_case_id")
        return self


class OrderService:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, tenant_id: str, value: OrderUpsert) -> dict[str, Any]:
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
                SELECT id, source_updated_at, payload_hash, version
                FROM commerce_orders
                WHERE tenant_id=? AND connector_id=? AND store_id=? AND external_order_id=?
                """,
                (tenant_id, value.connector_id, value.store_id, value.order_id),
            ).fetchone()
            if existing is not None:
                decision = decide_write(
                    existing_source_time=str(existing["source_updated_at"]),
                    existing_payload_hash=str(existing["payload_hash"]),
                    incoming_source_time=source_time,
                    incoming_payload_hash=payload_hash,
                )
                internal_id = str(existing["id"])
                if decision == "idempotent":
                    write_status = "idempotent"
            else:
                internal_id = f"order-{uuid.uuid4().hex}"

            if write_status == "applied":
                version = int(existing["version"]) + 1 if existing else 1
                conn.execute(
                    """
                    INSERT INTO commerce_orders(
                        id, tenant_id, connector_id, store_id, external_order_id,
                        order_status, payment_status, currency, total_amount,
                        placed_at, buyer_ref_hash, source_id, source_updated_at,
                        payload_hash, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, connector_id, store_id, external_order_id)
                    DO UPDATE SET
                        order_status=excluded.order_status,
                        payment_status=excluded.payment_status,
                        currency=excluded.currency, total_amount=excluded.total_amount,
                        placed_at=excluded.placed_at, buyer_ref_hash=excluded.buyer_ref_hash,
                        source_id=excluded.source_id,
                        source_updated_at=excluded.source_updated_at,
                        payload_hash=excluded.payload_hash,
                        version=excluded.version, updated_at=excluded.updated_at
                    """,
                    (
                        internal_id, tenant_id, value.connector_id, value.store_id,
                        value.order_id, value.order_status, value.payment_status,
                        value.currency, str(value.total_amount),
                        canonical_source_time(value.placed_at), value.buyer_ref_hash,
                        value.source_id, source_time, payload_hash, version, now, now,
                    ),
                )
                self._replace_children(conn, internal_id, value)
                conn.execute(
                    """
                    INSERT INTO commerce_order_events(
                        id, order_id, version, source_updated_at, payload_hash,
                        snapshot_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"order-event-{uuid.uuid4().hex}", internal_id, version,
                        source_time, payload_hash,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True), now,
                    ),
                )
        result = self._row_by_internal_id(tenant_id, internal_id)
        result["write_status"] = write_status
        return result

    def _replace_children(self, conn: Any, order_id: str, value: OrderUpsert) -> None:
        conn.execute("DELETE FROM commerce_order_lines WHERE order_id=?", (order_id,))
        conn.executemany(
            """
            INSERT INTO commerce_order_lines(
                id, order_id, external_line_id, sku_id, title, quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"order-line-{uuid.uuid4().hex}", order_id, line.line_id,
                    line.sku_id, line.title, line.quantity, str(line.unit_price),
                )
                for line in value.lines
            ],
        )
        conn.execute("DELETE FROM commerce_order_logistics WHERE order_id=?", (order_id,))
        if value.logistics is not None:
            logistics = value.logistics
            conn.execute(
                """
                INSERT INTO commerce_order_logistics(
                    order_id, carrier, tracking_no_masked, status,
                    last_event, last_event_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id, logistics.carrier, logistics.tracking_no_masked,
                    logistics.status, logistics.last_event,
                    canonical_source_time(logistics.last_event_at),
                ),
            )
        conn.execute("DELETE FROM commerce_after_sale_cases WHERE order_id=?", (order_id,))
        conn.executemany(
            """
            INSERT INTO commerce_after_sale_cases(
                id, order_id, external_case_id, case_type, status,
                requested_amount, approved_amount, reason_code, opened_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"after-sale-{uuid.uuid4().hex}", order_id, case.case_id,
                    case.case_type, case.status, str(case.requested_amount),
                    str(case.approved_amount), case.reason_code,
                    canonical_source_time(case.opened_at),
                    canonical_source_time(case.updated_at),
                )
                for case in value.after_sales
            ],
        )

    def list_orders(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        order_id: str | None = None,
        order_status: OrderStatus | None = None,
        limit: int = 100,
        service_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if order_id:
            conditions.append("external_order_id=?")
            params.append(order_id)
        if order_status:
            conditions.append("order_status=?")
            params.append(order_status)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id FROM commerce_orders
                WHERE {' AND '.join(conditions)}
                ORDER BY placed_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        views = [self._row_by_internal_id(tenant_id, str(row["id"])) for row in rows]
        if service_scope is None:
            return views
        service_by_order = self._customer_service_by_order(
            tenant_id,
            {(item["store_id"], item["order_id"]) for item in views},
            service_scope,
        )
        for item in views:
            item["customer_service"] = service_by_order.get(
                (item["store_id"], item["order_id"])
            )
        return views

    def _customer_service_by_order(
        self,
        tenant_id: str,
        order_keys: set[tuple[str, str]],
        scope: str,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        scope_condition = session_scope_condition(scope)
        if not order_keys:
            return {}
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT h.status, h.payload_json, h.started_at, h.updated_at,
                       s.source_type, q.queue_key
                FROM handoff_tasks h
                JOIN sessions s ON s.id=h.session_id
                JOIN handoff_queues q ON q.id=h.queue_id
                WHERE h.tenant_id=?
                  AND h.status IN ('proposed','accepted','working','input_required','review')
                  AND {scope_condition}
                ORDER BY h.updated_at DESC, h.id DESC
                """,
                (tenant_id,),
            ).fetchall()
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            context = payload.get("business_context")
            if not isinstance(context, dict):
                continue
            order_id = context.get("order_id")
            store_id = context.get("store_id")
            if not isinstance(order_id, str) or not isinstance(store_id, str):
                continue
            key = (store_id, order_id)
            if key not in order_keys or key in result:
                continue
            status, label = CUSTOMER_SERVICE_STATUS[str(row["status"])]
            result[key] = {
                "status": status,
                "label": label,
                "task_status": row["status"],
                "queue_key": row["queue_key"],
                "source_type": row["source_type"],
                "started_at": row["started_at"],
                "updated_at": row["updated_at"],
            }
        return result

    def history(
        self, tenant_id: str, order_id: str, *, store_id: str | None = None
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?", "external_order_id=?"]
        params: list[Any] = [tenant_id, order_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        with self.db.connect() as conn:
            orders = conn.execute(
                f"SELECT id FROM commerce_orders WHERE {' AND '.join(conditions)}",
                tuple(params),
            ).fetchall()
            if not orders:
                return []
            if len(orders) > 1:
                raise ValueError("ambiguous_order_id")
            rows = conn.execute(
                """
                SELECT version, source_updated_at, payload_hash, snapshot_json, created_at
                FROM commerce_order_events WHERE order_id=? ORDER BY version
                """,
                (orders[0]["id"],),
            ).fetchall()
        return [
            {
                "version": row["version"],
                "source_updated_at": row["source_updated_at"],
                "payload_hash": row["payload_hash"],
                "snapshot": json.loads(row["snapshot_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _row_by_internal_id(self, tenant_id: str, internal_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM commerce_orders WHERE id=? AND tenant_id=?",
                (internal_id, tenant_id),
            ).fetchone()
            if row is None:
                raise ValueError("order_not_found")
            lines = conn.execute(
                "SELECT * FROM commerce_order_lines WHERE order_id=? ORDER BY external_line_id",
                (internal_id,),
            ).fetchall()
            logistics = conn.execute(
                "SELECT * FROM commerce_order_logistics WHERE order_id=?",
                (internal_id,),
            ).fetchone()
            cases = conn.execute(
                "SELECT * FROM commerce_after_sale_cases WHERE order_id=? ORDER BY opened_at",
                (internal_id,),
            ).fetchall()
        return self._view(dict(row), [dict(item) for item in lines], dict(logistics) if logistics else None, [dict(item) for item in cases])

    @staticmethod
    def _view(
        row: dict[str, Any],
        lines: list[dict[str, Any]],
        logistics: dict[str, Any] | None,
        cases: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "id": row["id"],
            "connector_id": row["connector_id"],
            "store_id": row["store_id"],
            "order_id": row["external_order_id"],
            "order_status": row["order_status"],
            "payment_status": row["payment_status"],
            "currency": row["currency"],
            "total_amount": row["total_amount"],
            "placed_at": row["placed_at"],
            "buyer_ref_hash": row["buyer_ref_hash"],
            "lines": [
                {
                    "line_id": line["external_line_id"],
                    "sku_id": line["sku_id"],
                    "title": line["title"],
                    "quantity": line["quantity"],
                    "unit_price": line["unit_price"],
                }
                for line in lines
            ],
            "logistics": (
                {
                    "carrier": logistics["carrier"],
                    "tracking_no_masked": logistics["tracking_no_masked"],
                    "status": logistics["status"],
                    "last_event": logistics["last_event"],
                    "last_event_at": logistics["last_event_at"],
                }
                if logistics
                else None
            ),
            "after_sales": [
                {
                    "case_id": case["external_case_id"],
                    "case_type": case["case_type"],
                    "status": case["status"],
                    "requested_amount": case["requested_amount"],
                    "approved_amount": case["approved_amount"],
                    "reason_code": case["reason_code"],
                    "opened_at": case["opened_at"],
                    "updated_at": case["updated_at"],
                }
                for case in cases
            ],
            "source_id": row["source_id"],
            "source_updated_at": row["source_updated_at"],
            "data_quality": "traceable" if row["source_id"] else "source_id_missing",
            "version": row["version"],
            "updated_at": row["updated_at"],
        }
