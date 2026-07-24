from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Any, Callable

import pytest

from ecommerce_agent.business import (
    ContentDraftUpsert,
    FinanceReportQuery,
    MarketingDiagnosisQuery,
    MarketingPerformanceUpsert,
    OperatingExpenseUpsert,
    ReconciliationTaskTransition,
    SettlementStatementUpsert,
)
from ecommerce_agent.business.source_versioning import SourceVersionError
from ecommerce_agent.service import AgentService

from conftest import make_settings


TENANT_ID = "tenant-pressure"
OTHER_TENANT_ID = "tenant-isolated"
STORE_ID = "pressure-store"
SOURCE_TIME = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)
WORKERS = 16
REPLAY_COUNT = 128
CONTENT_DRAFT_COUNT = 64
RECONCILIATION_COUNT = 64
READ_COUNT = 240
ISOLATION_READ_COUNT = 64


def _marketing_metric() -> MarketingPerformanceUpsert:
    return MarketingPerformanceUpsert(
        connector_id="pressure-fixture",
        store_id=STORE_ID,
        campaign_id="campaign-pressure",
        metric_date=date(2026, 7, 24),
        campaign_name="Pressure replay campaign",
        channel="virtual",
        objective="conversion",
        status="active",
        spend=Decimal("50.00"),
        attributed_revenue=Decimal("0.00"),
        attributed_orders=0,
        impressions=10_000,
        clicks=200,
        source_type="virtual",
        source_updated_at=SOURCE_TIME,
        source_id="pressure-marketing-source",
    )


def _expense() -> OperatingExpenseUpsert:
    return OperatingExpenseUpsert(
        connector_id="pressure-fixture",
        store_id=STORE_ID,
        expense_key="platform-fee-pressure",
        occurred_on=date(2026, 7, 24),
        category="platform_fee",
        amount=Decimal("5.00"),
        source_type="virtual",
        source_updated_at=SOURCE_TIME,
        source_id="pressure-expense-source",
    )


def _statement() -> SettlementStatementUpsert:
    return SettlementStatementUpsert(
        connector_id="pressure-fixture",
        store_id=STORE_ID,
        statement_key="settlement-pressure",
        period_start=date(2026, 7, 24),
        period_end=date(2026, 7, 24),
        gross_sales=Decimal("100.00"),
        refund_amount=Decimal("0.00"),
        fee_amount=Decimal("5.00"),
        settlement_amount=Decimal("25.00"),
        source_type="virtual",
        source_updated_at=SOURCE_TIME,
        source_id="pressure-statement-source",
    )


def _content_draft(index: int) -> ContentDraftUpsert:
    return ContentDraftUpsert(
        draft_key=f"pressure-draft-{index}",
        store_id=STORE_ID,
        content_type="campaign_copy",
        title=f"Pressure draft {index}",
        body="Draft retained for human review only.",
        sku_ids=[f"PRESSURE-SKU-{index}"],
        declared_prices={f"PRESSURE-SKU-{index}": Decimal("99.00")},
        source_type="virtual",
        source_id=f"pressure-draft-source-{index}",
        expected_record_version=0,
    )


def _submit_all(
    executor: ThreadPoolExecutor,
    jobs: list[tuple[str, Callable[[], Any]]],
) -> list[tuple[str, Any]]:
    futures = [(name, executor.submit(operation)) for name, operation in jobs]
    return [(name, future.result()) for name, future in futures]


def test_marketing_finance_pressure_replay_concurrency_and_isolation(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    marketing = service.operations.marketing
    finance = service.operations.finance
    marketing_value = _marketing_metric()
    expense_value = _expense()
    statement_value = _statement()
    diagnosis_query = MarketingDiagnosisQuery(store_id=STORE_ID)
    report_query = FinanceReportQuery(
        store_id=STORE_ID,
        start_date=date(2026, 7, 24),
        end_date=date(2026, 7, 24),
    )
    try:
        write_jobs: list[tuple[str, Callable[[], Any]]] = []
        write_jobs.extend(
            ("marketing", lambda: marketing.upsert_performance(TENANT_ID, marketing_value))
            for _ in range(REPLAY_COUNT)
        )
        write_jobs.extend(
            ("expense", lambda: finance.upsert_expense(TENANT_ID, expense_value))
            for _ in range(REPLAY_COUNT)
        )
        write_jobs.extend(
            ("statement", lambda: finance.upsert_statement(TENANT_ID, statement_value))
            for _ in range(REPLAY_COUNT)
        )
        write_jobs.extend(
            (
                "content_draft",
                lambda index=index: marketing.save_content_draft(TENANT_ID, _content_draft(index)),
            )
            for index in range(CONTENT_DRAFT_COUNT)
        )

        started = perf_counter()
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            write_results = _submit_all(executor, write_jobs)
        write_seconds = perf_counter() - started

        write_statuses = {
            name: Counter(
                str(item["write_status"])
                for result_name, item in write_results
                if result_name == name
            )
            for name in ("marketing", "expense", "statement")
        }
        assert write_statuses["marketing"] == Counter(applied=1, idempotent=REPLAY_COUNT - 1)
        assert write_statuses["expense"] == Counter(applied=1, idempotent=REPLAY_COUNT - 1)
        assert write_statuses["statement"] == Counter(applied=1, idempotent=REPLAY_COUNT - 1)
        content_drafts = [
            item for name, item in write_results if name == "content_draft"
        ]
        assert len(content_drafts) == CONTENT_DRAFT_COUNT
        assert all(item["publication_allowed"] is False for item in content_drafts)
        assert all(item["fact_check"]["passed"] is False for item in content_drafts)

        with pytest.raises(SourceVersionError, match="source_version_conflict"):
            marketing.upsert_performance(
                TENANT_ID,
                marketing_value.model_copy(update={"spend": Decimal("51.00")}),
            )
        with pytest.raises(SourceVersionError, match="stale_source_version"):
            finance.upsert_expense(
                TENANT_ID,
                expense_value.model_copy(
                    update={"source_updated_at": SOURCE_TIME - timedelta(seconds=1)}
                ),
            )
        with pytest.raises(SourceVersionError, match="source_version_conflict"):
            finance.upsert_statement(
                TENANT_ID,
                statement_value.model_copy(update={"settlement_amount": Decimal("26.00")}),
            )
        with pytest.raises(ValueError, match="content_draft_version_conflict"):
            marketing.save_content_draft(TENANT_ID, _content_draft(0))

        started = perf_counter()
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            reconciliation_results = list(
                executor.map(
                    lambda _: finance.run_reconciliation(
                        TENANT_ID,
                        report_query,
                        tolerance_amount=Decimal("1.00"),
                    ),
                    range(RECONCILIATION_COUNT),
                )
            )
        reconciliation_seconds = perf_counter() - started
        assert sum(item["tasks_created"] for item in reconciliation_results) == 1
        assert sum(item["tasks_updated"] for item in reconciliation_results) == RECONCILIATION_COUNT - 1

        tasks = finance.list_reconciliation_tasks(TENANT_ID, store_id=STORE_ID)
        assert len(tasks) == 1
        task = tasks[0]
        assert task["difference_amount"] == "30.00"
        task_version = int(task["record_version"])

        def read_once(index: int) -> tuple[str, Any]:
            operation = index % 4
            if operation == 0:
                return "diagnosis", marketing.diagnose(TENANT_ID, diagnosis_query)
            if operation == 1:
                return "profit", finance.profit_report(TENANT_ID, report_query)
            if operation == 2:
                return "tasks", finance.list_reconciliation_tasks(TENANT_ID, store_id=STORE_ID)
            return "drafts", marketing.list_content_drafts(TENANT_ID, store_id=STORE_ID)

        started = perf_counter()
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            read_results = list(executor.map(read_once, range(READ_COUNT)))
        read_seconds = perf_counter() - started
        for name, value in read_results:
            if name == "diagnosis":
                assert value["data_quality"]["record_count"] == 1
                assert value["findings"][0]["code"] == "high_spend_no_orders"
            elif name == "profit":
                assert value["expense_total"] == "5.00"
                assert value["data_quality"]["financial_statement"] is False
            elif name == "tasks":
                assert len(value) == 1
            else:
                assert len(value) == CONTENT_DRAFT_COUNT

        def transition_once(_: int) -> str:
            try:
                finance.transition_reconciliation_task(
                    TENANT_ID,
                    task["id"],
                    ReconciliationTaskTransition(
                        target_status="reviewing",
                        expected_record_version=task_version,
                        note="Concurrent manual review claim.",
                    ),
                    actor="pressure-tester",
                )
            except ValueError as exc:
                return str(exc)
            return "applied"

        with ThreadPoolExecutor(max_workers=2) as executor:
            transition_outcomes = list(executor.map(transition_once, range(2)))
        assert Counter(transition_outcomes) == Counter(
            applied=1, reconciliation_task_version_conflict=1
        )

        def isolation_read(_: int) -> tuple[int, int, int, int, int]:
            diagnosis = marketing.diagnose(OTHER_TENANT_ID, diagnosis_query)
            profit = finance.profit_report(OTHER_TENANT_ID, report_query)
            return (
                diagnosis["data_quality"]["record_count"],
                profit["record_counts"]["expenses"],
                len(finance.list_reconciliation_tasks(OTHER_TENANT_ID, store_id=STORE_ID)),
                len(marketing.list_content_drafts(OTHER_TENANT_ID, store_id=STORE_ID)),
                len(finance.list_statements(OTHER_TENANT_ID, store_id=STORE_ID)),
            )

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            isolation_results = list(executor.map(isolation_read, range(ISOLATION_READ_COUNT)))
        assert set(isolation_results) == {(0, 0, 0, 0, 0)}

        completed_operations = (
            len(write_results)
            + len(reconciliation_results)
            + len(read_results)
            + len(transition_outcomes)
            + len(isolation_results)
        )
        pressure_report = {
            "contract": "marketing-finance-pressure-v1",
            "workers": WORKERS,
            "completed_operations": completed_operations,
            "concurrent_writes": len(write_results),
            "reconciliation_runs": len(reconciliation_results),
            "concurrent_reads": len(read_results),
            "tenant_isolation_reads": len(isolation_results),
            "write_statuses": {name: dict(statuses) for name, statuses in write_statuses.items()},
            "durations_seconds": {
                "writes": round(write_seconds, 3),
                "reconciliation": round(reconciliation_seconds, 3),
                "reads": round(read_seconds, 3),
            },
            "reconciliation_task": {
                "count": len(tasks),
                "difference_amount": task["difference_amount"],
                "transition_outcomes": dict(Counter(transition_outcomes)),
            },
            "boundaries": {
                "content_publication_allowed": False,
                "financial_statement": False,
                "tenant_isolation": "verified",
            },
        }
        print("MARKETING_FINANCE_PRESSURE_REPORT=" + json.dumps(pressure_report, sort_keys=True))
    finally:
        service.close()
