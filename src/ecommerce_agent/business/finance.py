from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..database import Database, utc_now
from .source_versioning import canonical_source_time, decide_write, payload_digest


ExpenseCategory = Literal[
    "product_cost",
    "advertising",
    "platform_fee",
    "logistics",
    "fulfillment",
    "refund",
    "other",
]
FinancialSourceType = Literal["virtual", "file_import"]
ReconciliationStatus = Literal["open", "reviewing", "resolved", "ignored"]


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class OperatingExpenseUpsert(BaseModel):
    """A source-versioned operating expense fact, not a general-ledger posting."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    expense_key: str = Field(min_length=1, max_length=128)
    occurred_on: date
    category: ExpenseCategory
    amount: Decimal = Field(ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    source_type: FinancialSourceType
    source_updated_at: datetime
    source_id: str | None = Field(default=None, max_length=256)

    @field_validator("source_updated_at")
    @classmethod
    def require_aware_source_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value


class SettlementStatementUpsert(BaseModel):
    """An imported settlement statement used for operational reconciliation only."""

    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    statement_key: str = Field(min_length=1, max_length=128)
    period_start: date
    period_end: date
    gross_sales: Decimal = Field(ge=0)
    refund_amount: Decimal = Field(ge=0)
    fee_amount: Decimal = Field(ge=0)
    settlement_amount: Decimal = Field(ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    source_type: FinancialSourceType
    source_updated_at: datetime
    source_id: str | None = Field(default=None, max_length=256)

    @field_validator("source_updated_at")
    @classmethod
    def require_aware_source_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "SettlementStatementUpsert":
        if self.period_start > self.period_end:
            raise ValueError("statement_date_range_invalid")
        return self


class FinanceReportQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str | None = Field(default=None, max_length=128)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "FinanceReportQuery":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("finance_date_range_invalid")
        return self


class ReconciliationTaskTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_status: Literal["reviewing", "resolved", "ignored"]
    expected_record_version: int = Field(ge=1)
    note: str = Field(min_length=2, max_length=1000)


class FinanceService:
    """Management-profit and reconciliation workflow. It does not post accounting entries."""

    _core_cost_categories = {"product_cost", "advertising", "platform_fee", "logistics"}

    def __init__(self, db: Database):
        self.db = db

    def upsert_expense(self, tenant_id: str, value: OperatingExpenseUpsert) -> dict[str, Any]:
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
                FROM operating_expenses
                WHERE tenant_id=? AND connector_id=? AND store_id=? AND expense_key=?
                """,
                (tenant_id, value.connector_id, value.store_id, value.expense_key),
            ).fetchone()
            expense_id = str(existing["id"]) if existing else f"expense-{uuid.uuid4().hex}"
            if existing is not None:
                decision = decide_write(
                    existing_source_time=str(existing["source_updated_at"]),
                    existing_payload_hash=str(existing["payload_hash"]),
                    incoming_source_time=source_time,
                    incoming_payload_hash=payload_hash,
                )
                if decision == "idempotent":
                    write_status = "idempotent"
            if write_status == "applied":
                version = int(existing["version"]) + 1 if existing else 1
                conn.execute(
                    """
                    INSERT INTO operating_expenses(
                        id, tenant_id, connector_id, store_id, expense_key, occurred_on,
                        category, amount, currency, source_type, source_id, source_updated_at,
                        payload_hash, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, connector_id, store_id, expense_key) DO UPDATE SET
                        occurred_on=excluded.occurred_on, category=excluded.category,
                        amount=excluded.amount, currency=excluded.currency,
                        source_type=excluded.source_type, source_id=excluded.source_id,
                        source_updated_at=excluded.source_updated_at,
                        payload_hash=excluded.payload_hash, version=excluded.version,
                        updated_at=excluded.updated_at
                    """,
                    (
                        expense_id,
                        tenant_id,
                        value.connector_id,
                        value.store_id,
                        value.expense_key,
                        value.occurred_on.isoformat(),
                        value.category,
                        _money(value.amount),
                        value.currency,
                        value.source_type,
                        value.source_id,
                        source_time,
                        payload_hash,
                        version,
                        now,
                        now,
                    ),
                )
        result = self._expense_by_id(tenant_id, expense_id)
        result["write_status"] = write_status
        return result

    def upsert_statement(
        self, tenant_id: str, value: SettlementStatementUpsert
    ) -> dict[str, Any]:
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
                FROM settlement_statements
                WHERE tenant_id=? AND connector_id=? AND store_id=? AND statement_key=?
                """,
                (tenant_id, value.connector_id, value.store_id, value.statement_key),
            ).fetchone()
            statement_id = str(existing["id"]) if existing else f"statement-{uuid.uuid4().hex}"
            if existing is not None:
                decision = decide_write(
                    existing_source_time=str(existing["source_updated_at"]),
                    existing_payload_hash=str(existing["payload_hash"]),
                    incoming_source_time=source_time,
                    incoming_payload_hash=payload_hash,
                )
                if decision == "idempotent":
                    write_status = "idempotent"
            if write_status == "applied":
                version = int(existing["version"]) + 1 if existing else 1
                conn.execute(
                    """
                    INSERT INTO settlement_statements(
                        id, tenant_id, connector_id, store_id, statement_key,
                        period_start, period_end, gross_sales, refund_amount, fee_amount,
                        settlement_amount, currency, source_type, source_id,
                        source_updated_at, payload_hash, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, connector_id, store_id, statement_key) DO UPDATE SET
                        period_start=excluded.period_start, period_end=excluded.period_end,
                        gross_sales=excluded.gross_sales, refund_amount=excluded.refund_amount,
                        fee_amount=excluded.fee_amount, settlement_amount=excluded.settlement_amount,
                        currency=excluded.currency, source_type=excluded.source_type,
                        source_id=excluded.source_id, source_updated_at=excluded.source_updated_at,
                        payload_hash=excluded.payload_hash, version=excluded.version,
                        updated_at=excluded.updated_at
                    """,
                    (
                        statement_id,
                        tenant_id,
                        value.connector_id,
                        value.store_id,
                        value.statement_key,
                        value.period_start.isoformat(),
                        value.period_end.isoformat(),
                        _money(value.gross_sales),
                        _money(value.refund_amount),
                        _money(value.fee_amount),
                        _money(value.settlement_amount),
                        value.currency,
                        value.source_type,
                        value.source_id,
                        source_time,
                        payload_hash,
                        version,
                        now,
                        now,
                    ),
                )
        result = self._statement_by_id(tenant_id, statement_id)
        result["write_status"] = write_status
        return result

    def list_expenses(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = 500,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if start_date:
            conditions.append("occurred_on>=?")
            params.append(start_date.isoformat())
        if end_date:
            conditions.append("occurred_on<=?")
            params.append(end_date.isoformat())
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM operating_expenses WHERE {' AND '.join(conditions)}
                ORDER BY occurred_on DESC, expense_key ASC {limit_clause}
                """,
                tuple(params),
            ).fetchall()
        return [self._expense_view(dict(row)) for row in rows]

    def list_statements(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM settlement_statements WHERE {' AND '.join(conditions)}
                ORDER BY period_end DESC, statement_key ASC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._statement_view(dict(row)) for row in rows]

    def profit_report(self, tenant_id: str, query: FinanceReportQuery) -> dict[str, Any]:
        order_conditions = ["tenant_id=?", "payment_status IN ('paid','partially_refunded','refunded')"]
        order_params: list[Any] = [tenant_id]
        if query.store_id:
            order_conditions.append("store_id=?")
            order_params.append(query.store_id)
        if query.start_date:
            order_conditions.append("substr(placed_at, 1, 10)>=?")
            order_params.append(query.start_date.isoformat())
        if query.end_date:
            order_conditions.append("substr(placed_at, 1, 10)<=?")
            order_params.append(query.end_date.isoformat())
        with self.db.connect() as conn:
            orders = conn.execute(
                f"""
                SELECT id, total_amount, currency FROM commerce_orders
                WHERE {' AND '.join(order_conditions)}
                """,
                tuple(order_params),
            ).fetchall()
            order_ids = [str(item["id"]) for item in orders]
            refunds = Decimal("0")
            if order_ids:
                placeholders = ",".join("?" for _ in order_ids)
                refund_rows = conn.execute(
                    f"""
                    SELECT approved_amount FROM commerce_after_sale_cases
                    WHERE order_id IN ({placeholders})
                      AND status IN ('approved','returning','completed')
                    """,
                    tuple(order_ids),
                ).fetchall()
                refunds = sum((Decimal(str(row["approved_amount"])) for row in refund_rows), Decimal("0"))
        expenses = self.list_expenses(
            tenant_id,
            store_id=query.store_id,
            start_date=query.start_date,
            end_date=query.end_date,
            limit=None,
        )
        currency_values = {str(row["currency"]) for row in orders} | {
            str(row["currency"]) for row in expenses
        }
        currency = "CNY" if not currency_values else next(iter(currency_values))
        currency_consistent = len(currency_values) <= 1
        gross_sales = sum((Decimal(str(row["total_amount"])) for row in orders), Decimal("0"))
        expense_by_category: dict[str, Decimal] = {}
        for item in expenses:
            category = str(item["category"])
            expense_by_category[category] = expense_by_category.get(category, Decimal("0")) + Decimal(item["amount"])
        expense_total = sum(expense_by_category.values(), Decimal("0"))
        net_sales = gross_sales - refunds
        management_profit = net_sales - expense_total
        present_categories = set(expense_by_category)
        missing_categories = sorted(self._core_cost_categories - present_categories)
        source_types = sorted(
            {str(item["source_type"]) for item in expenses}
        )
        return {
            "period": {
                "store_id": query.store_id,
                "start_date": query.start_date.isoformat() if query.start_date else None,
                "end_date": query.end_date.isoformat() if query.end_date else None,
            },
            "currency": currency,
            "currency_consistent": currency_consistent,
            "gross_sales": _money(gross_sales),
            "approved_refunds": _money(refunds),
            "net_sales": _money(net_sales),
            "expenses_by_category": {
                category: _money(amount) for category, amount in sorted(expense_by_category.items())
            },
            "expense_total": _money(expense_total),
            "management_profit": _money(management_profit),
            "record_counts": {"orders": len(orders), "expenses": len(expenses)},
            "data_quality": {
                "missing_cost_categories": missing_categories,
                "source_types": source_types,
                "virtual_only": bool(expenses) and source_types == ["virtual"],
                "financial_statement": False,
                "management_estimate": True,
                "currency_consistent": currency_consistent,
            },
            "scope_boundary": "Management estimate only. It is not a general ledger, tax calculation, or settlement instruction.",
        }

    def run_reconciliation(
        self,
        tenant_id: str,
        query: FinanceReportQuery,
        *,
        tolerance_amount: Decimal = Decimal("1.00"),
    ) -> dict[str, Any]:
        if tolerance_amount < 0:
            raise ValueError("reconciliation_tolerance_invalid")
        statements = self.list_statements(tenant_id, store_id=query.store_id)
        created = 0
        updated = 0
        within_tolerance = 0
        task_ids: list[str] = []
        for statement in statements:
            if query.start_date and statement["period_end"] < query.start_date.isoformat():
                continue
            if query.end_date and statement["period_start"] > query.end_date.isoformat():
                continue
            statement_query = FinanceReportQuery(
                store_id=statement["store_id"],
                start_date=date.fromisoformat(statement["period_start"]),
                end_date=date.fromisoformat(statement["period_end"]),
            )
            report = self.profit_report(tenant_id, statement_query)
            platform_fees = Decimal(report["expenses_by_category"].get("platform_fee", "0"))
            expected = Decimal(report["gross_sales"]) - Decimal(report["approved_refunds"]) - platform_fees
            reported = Decimal(statement["settlement_amount"])
            difference = reported - expected
            if abs(difference) <= tolerance_amount:
                within_tolerance += 1
                continue
            result = self._upsert_reconciliation_task(
                tenant_id,
                statement=statement,
                expected=expected,
                reported=reported,
                difference=difference,
                tolerance=tolerance_amount,
            )
            task_ids.append(result["id"])
            if result["write_status"] == "created":
                created += 1
            elif result["write_status"] == "updated":
                updated += 1
        return {
            "statements_evaluated": len(statements),
            "tasks_created": created,
            "tasks_updated": updated,
            "within_tolerance": within_tolerance,
            "task_ids": task_ids,
            "action_boundary": "Only reconciliation tasks are created or refreshed. No ledger, tax, payout, or refund is modified.",
        }

    def list_reconciliation_tasks(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        status: ReconciliationStatus | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if status:
            conditions.append("status=?")
            params.append(status)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT task.*, statement.statement_key, statement.period_start, statement.period_end
                FROM reconciliation_tasks task
                JOIN settlement_statements statement ON statement.id=task.statement_id
                WHERE {' AND '.join(f'task.{item}' if item != 'tenant_id=?' else 'task.tenant_id=?' for item in conditions)}
                ORDER BY task.updated_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._task_view(dict(row)) for row in rows]

    def transition_reconciliation_task(
        self,
        tenant_id: str,
        task_id: str,
        value: ReconciliationTaskTransition,
        *,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM reconciliation_tasks WHERE tenant_id=? AND id=?",
                (tenant_id, task_id),
            ).fetchone()
            if row is None:
                raise ValueError("reconciliation_task_not_found")
            if int(row["record_version"]) != value.expected_record_version:
                raise ValueError("reconciliation_task_version_conflict")
            if str(row["status"]) in {"resolved", "ignored"}:
                raise ValueError("reconciliation_task_already_closed")
            conn.execute(
                """
                UPDATE reconciliation_tasks
                SET status=?, note=?, record_version=record_version+1, updated_at=?
                WHERE id=?
                """,
                (value.target_status, value.note, now, task_id),
            )
        self.db.audit(
            f"finance.reconciliation_task.{value.target_status}",
            actor,
            task_id,
            {"note": value.note},
            tenant_id,
        )
        return self._task_by_id(tenant_id, task_id)

    def _upsert_reconciliation_task(
        self,
        tenant_id: str,
        *,
        statement: dict[str, Any],
        expected: Decimal,
        reported: Decimal,
        difference: Decimal,
        tolerance: Decimal,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT id, status, record_version FROM reconciliation_tasks
                WHERE tenant_id=? AND statement_id=?
                """,
                (tenant_id, statement["id"]),
            ).fetchone()
            task_id = str(existing["id"]) if existing else f"reconciliation-{uuid.uuid4().hex}"
            if existing is None:
                write_status = "created"
                conn.execute(
                    """
                    INSERT INTO reconciliation_tasks(
                        id, tenant_id, statement_id, store_id, status, expected_settlement,
                        reported_settlement, difference_amount, tolerance_amount, note,
                        record_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, NULL, 1, ?, ?)
                    """,
                    (
                        task_id,
                        tenant_id,
                        statement["id"],
                        statement["store_id"],
                        _money(expected),
                        _money(reported),
                        _money(difference),
                        _money(tolerance),
                        now,
                        now,
                    ),
                )
            elif str(existing["status"]) in {"resolved", "ignored"}:
                write_status = "unchanged_closed"
            else:
                write_status = "updated"
                conn.execute(
                    """
                    UPDATE reconciliation_tasks
                    SET expected_settlement=?, reported_settlement=?, difference_amount=?,
                        tolerance_amount=?, record_version=record_version+1, updated_at=?
                    WHERE id=?
                    """,
                    (
                        _money(expected),
                        _money(reported),
                        _money(difference),
                        _money(tolerance),
                        now,
                        task_id,
                    ),
                )
        result = self._task_by_id(tenant_id, task_id)
        result["write_status"] = write_status
        return result

    def _expense_by_id(self, tenant_id: str, expense_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM operating_expenses WHERE tenant_id=? AND id=?",
                (tenant_id, expense_id),
            ).fetchone()
        if row is None:
            raise ValueError("operating_expense_not_found")
        return self._expense_view(dict(row))

    def _statement_by_id(self, tenant_id: str, statement_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM settlement_statements WHERE tenant_id=? AND id=?",
                (tenant_id, statement_id),
            ).fetchone()
        if row is None:
            raise ValueError("settlement_statement_not_found")
        return self._statement_view(dict(row))

    def _task_by_id(self, tenant_id: str, task_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT task.*, statement.statement_key, statement.period_start, statement.period_end
                FROM reconciliation_tasks task
                JOIN settlement_statements statement ON statement.id=task.statement_id
                WHERE task.tenant_id=? AND task.id=?
                """,
                (tenant_id, task_id),
            ).fetchone()
        if row is None:
            raise ValueError("reconciliation_task_not_found")
        return self._task_view(dict(row))

    @staticmethod
    def _expense_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "connector_id": row["connector_id"],
            "store_id": row["store_id"],
            "expense_key": row["expense_key"],
            "occurred_on": row["occurred_on"],
            "category": row["category"],
            "amount": _money(Decimal(str(row["amount"]))),
            "currency": row["currency"],
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "source_updated_at": row["source_updated_at"],
            "version": int(row["version"]),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _statement_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "connector_id": row["connector_id"],
            "store_id": row["store_id"],
            "statement_key": row["statement_key"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "gross_sales": _money(Decimal(str(row["gross_sales"]))),
            "refund_amount": _money(Decimal(str(row["refund_amount"]))),
            "fee_amount": _money(Decimal(str(row["fee_amount"]))),
            "settlement_amount": _money(Decimal(str(row["settlement_amount"]))),
            "currency": row["currency"],
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "source_updated_at": row["source_updated_at"],
            "version": int(row["version"]),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _task_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "statement_id": row["statement_id"],
            "statement_key": row["statement_key"],
            "store_id": row["store_id"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "status": row["status"],
            "expected_settlement": _money(Decimal(str(row["expected_settlement"]))),
            "reported_settlement": _money(Decimal(str(row["reported_settlement"]))),
            "difference_amount": _money(Decimal(str(row["difference_amount"]))),
            "tolerance_amount": _money(Decimal(str(row["tolerance_amount"]))),
            "note": row["note"],
            "record_version": int(row["record_version"]),
            "updated_at": row["updated_at"],
        }
