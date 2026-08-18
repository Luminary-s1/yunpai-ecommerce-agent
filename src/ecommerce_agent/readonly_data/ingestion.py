from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from ..business.catalog import CatalogItemUpsert, CatalogService
from ..business.finance import FinanceService, SettlementStatementUpsert
from ..business.inventory import InventoryBalanceUpsert, InventoryService
from ..business.marketing import MarketingPerformanceUpsert, MarketingService
from ..business.ops_assistant import OpsAssistantService, OpsOperationRecordUpsert
from ..business.orders import (
    AfterSaleCaseInput,
    LogisticsSnapshotInput,
    OrderLineInput,
    OrderService,
    OrderUpsert,
)
from ..business.source_versioning import SourceVersionError
from ..database import Database
from .adapters import (
    REPORT_ADAPTERS,
    CatalogSnapshotRow,
    FulfillmentSnapshotRow,
    InventorySnapshotRow,
    MarketingDailyRow,
    OperationsDailyRow,
    OrderSnapshotRow,
    RefundSnapshotRow,
    ReportAdapter,
    ReportDomain,
    ReportFileFormat,
    ReportImportJob,
    ReportImportRequest,
    SettlementStatementRow,
)
from .contracts import (
    DataScope,
    EvidenceState,
    FieldEvidenceInput,
    ImportManifestInput,
    ImportReference,
    ReferenceKind,
    RowDisposition,
    RowIsolationIssue,
    content_digest,
    sanitize_report_row,
    schema_fingerprint,
)
from .file_parser import ParsedReport, ParsedReportRow, parse_report_file
from .service import ReadonlyDataService


_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{1,127}$")


@dataclass(frozen=True, slots=True)
class _PreparedRow:
    row_number: int
    raw_digest: str
    value: BaseModel


class ReadonlyReportIngestionService:
    """WP2 report boundary: parse, isolate, then call existing domain services."""

    def __init__(self, db: Database):
        self.db = db
        self.readonly = ReadonlyDataService(db)
        self.catalog = CatalogService(db)
        self.inventory = InventoryService(db)
        self.orders = OrderService(db)
        self.operations = OpsAssistantService(db)
        self.marketing = MarketingService(db)
        self.finance = FinanceService(db)

    def ingest(
        self,
        tenant_id: str,
        request: ReportImportRequest,
        content: bytes,
    ) -> dict[str, Any]:
        adapter = REPORT_ADAPTERS.get(request.report_type, request.mapping_version)
        if request.file_format not in adapter.formats:
            raise ValueError("report_file_format_not_supported")
        parsed = parse_report_file(
            content,
            request.file_format,
            sheet_name=request.sheet_name,
        )
        digest = content_digest(content)
        manifest_input = self._manifest_input(
            request,
            adapter,
            parsed,
            digest=digest,
        )
        trace = self._trace(request, adapter, parsed)

        # Database._write_lock is an RLock. Holding it across preflight, domain
        # services and manifest write prevents a same-process source-version race;
        # each public domain service still owns its own database transaction.
        with self.db._write_lock:
            existing = self.readonly.preflight_import(tenant_id, manifest_input)
            if existing is not None:
                existing = dict(existing)
                existing["write_status"] = "idempotent"
                return self._result(
                    tenant_id,
                    adapter,
                    existing,
                    trace=trace,
                    domain_writes={"applied": 0, "idempotent": 0},
                    replayed=True,
                )

            prepared, issues = self._prepare_rows(adapter, parsed, request)
            successful, domain_issues, writes = self._write_domain_rows(
                tenant_id,
                adapter,
                prepared,
                request,
                digest=digest,
            )
            issues.extend(domain_issues)
            issues.sort(key=lambda issue: issue.row_number)
            manifest = self.readonly.record_import(
                tenant_id,
                manifest_input,
                row_issues=issues,
            )
            self._record_field_evidence(
                tenant_id,
                request,
                manifest,
                successful,
            )
        return self._result(
            tenant_id,
            adapter,
            manifest,
            trace=trace,
            domain_writes=writes,
            replayed=False,
        )

    def ingest_batch(
        self, tenant_id: str, jobs: Iterable[ReportImportJob]
    ) -> dict[str, Any]:
        materialized = list(jobs)
        if not materialized:
            raise ValueError("report_import_batch_empty")
        reports: list[dict[str, Any]] = []
        for job in materialized:
            try:
                reports.append(self.ingest(tenant_id, job.request, job.content))
            except Exception as exc:  # one file must not erase another domain's success
                reports.append(
                    {
                        "report_type": job.request.report_type,
                        "mapping_version": job.request.mapping_version,
                        "status": "failed",
                        "error_code": self._error_code(exc),
                    }
                )
        succeeded = sum(item["status"] != "failed" for item in reports)
        failed = len(reports) - succeeded
        if succeeded == 0:
            status = "failed"
        elif failed or any(item["status"] == "partial" for item in reports):
            status = "partial"
        else:
            status = "passed"
        return {
            "status": status,
            "reports_total": len(reports),
            "reports_succeeded": succeeded,
            "reports_failed": failed,
            "reports": reports,
        }

    @staticmethod
    def _manifest_input(
        request: ReportImportRequest,
        adapter: ReportAdapter,
        parsed: ParsedReport,
        *,
        digest: str,
    ) -> ImportManifestInput:
        sheet = (
            hashlib.sha256(parsed.sheet_name.encode()).hexdigest()[:16]
            if parsed.sheet_name is not None
            else "none"
        )
        receipt = (
            f"readonly-adapter:{adapter.report_type}:{adapter.mapping_version}:"
            f"format:{request.file_format.value}:sheet:{sheet}:"
            f"timezone:{request.source_timezone}:grain:{adapter.grain}:"
            f"unit:{adapter.amount_unit}"
        )
        return ImportManifestInput(
            store_id=request.store_id,
            source_kind=request.source_kind,
            source_system=request.source_system,
            report_type=adapter.report_type,
            report_period=request.report_period,
            exported_at=request.exported_at,
            schema_fingerprint=schema_fingerprint(parsed.headers),
            content_digest=digest,
            mapping_version=adapter.mapping_version,
            parsed_rows=len(parsed.rows),
            data_as_of=request.data_as_of,
            references=(
                ImportReference(
                    kind=ReferenceKind.RAW_FILE,
                    reference=request.storage_ref,
                    content_digest=digest,
                ),
                ImportReference(
                    kind=ReferenceKind.SOURCE_RECEIPT,
                    reference=receipt,
                ),
            ),
        )

    def _prepare_rows(
        self,
        adapter: ReportAdapter,
        parsed: ParsedReport,
        request: ReportImportRequest,
    ) -> tuple[list[_PreparedRow], list[RowIsolationIssue]]:
        candidates: list[_PreparedRow] = []
        issues: list[RowIsolationIssue] = []
        for row in parsed.rows:
            raw_digest = self._raw_row_digest(row)
            if row.error_code is not None:
                issues.append(
                    self._issue(
                        row.row_number,
                        raw_digest,
                        disposition=RowDisposition.REJECTED,
                        reason=row.error_code,
                    )
                )
                continue
            try:
                sanitized = sanitize_report_row(adapter.policy, row.values)
                payload = adapter.normalize_values(
                    sanitized.downstream_payload(),
                    excel_date_system=parsed.excel_date_system,
                )
            except (TypeError, ValueError):
                issues.append(
                    self._issue(
                        row.row_number,
                        raw_digest,
                        disposition=RowDisposition.REJECTED,
                        reason="invalid_report_row",
                    )
                )
                continue
            if payload.get("store_id") != request.store_id:
                issues.append(
                    self._issue(
                        row.row_number,
                        raw_digest,
                        disposition=RowDisposition.QUARANTINED,
                        reason="cross_store_row",
                        field_keys=("store_id",),
                    )
                )
                continue
            try:
                value = adapter.row_model.model_validate(payload)
            except ValidationError as exc:
                issues.append(
                    self._issue(
                        row.row_number,
                        raw_digest,
                        disposition=RowDisposition.REJECTED,
                        reason="invalid_report_row",
                        field_keys=self._validation_fields(exc, adapter),
                    )
                )
                continue
            candidates.append(
                _PreparedRow(
                    row_number=row.row_number,
                    raw_digest=raw_digest,
                    value=value,
                )
            )

        by_identity: dict[tuple[Any, ...], list[_PreparedRow]] = defaultdict(list)
        for row in candidates:
            by_identity[
                tuple(getattr(row.value, field) for field in adapter.identity_fields)
            ].append(row)
        duplicated_numbers = {
            row.row_number
            for rows in by_identity.values()
            if len(rows) > 1
            for row in rows
        }
        for row in candidates:
            if row.row_number in duplicated_numbers:
                issues.append(
                    self._issue(
                        row.row_number,
                        row.raw_digest,
                        disposition=RowDisposition.QUARANTINED,
                        reason="duplicate_report_identity",
                        field_keys=adapter.identity_fields,
                    )
                )
        accepted = [
            row for row in candidates if row.row_number not in duplicated_numbers
        ]
        return accepted, issues

    def _write_domain_rows(
        self,
        tenant_id: str,
        adapter: ReportAdapter,
        rows: list[_PreparedRow],
        request: ReportImportRequest,
        *,
        digest: str,
    ) -> tuple[list[_PreparedRow], list[RowIsolationIssue], dict[str, int]]:
        writes = {"applied": 0, "idempotent": 0}
        if adapter.domain is ReportDomain.ORDERS:
            return self._write_orders(
                tenant_id, rows, request, digest=digest, writes=writes
            )
        if adapter.domain is ReportDomain.REFUNDS:
            return self._write_refunds(
                tenant_id, rows, request, digest=digest, writes=writes
            )

        successful: list[_PreparedRow] = []
        issues: list[RowIsolationIssue] = []
        for row in sorted(rows, key=self._prepared_identity):
            try:
                result = self._write_one(
                    tenant_id,
                    adapter,
                    row,
                    request,
                    digest=digest,
                )
            except (SourceVersionError, ValueError) as exc:
                issues.append(self._domain_issue(row, exc))
                continue
            writes[str(result["write_status"])] += 1
            successful.append(row)
        return successful, issues, writes

    def _write_one(
        self,
        tenant_id: str,
        adapter: ReportAdapter,
        row: _PreparedRow,
        request: ReportImportRequest,
        *,
        digest: str,
    ) -> dict[str, Any]:
        source_id = self._source_id(adapter.report_type, digest, row.value)
        source_time = request.data_as_of
        if adapter.domain is ReportDomain.CATALOG:
            value = CatalogSnapshotRow.model_validate(row.value)
            attributes = (
                {"merchant_code": value.merchant_code}
                if value.merchant_code is not None
                else {}
            )
            return self.catalog.upsert(
                tenant_id,
                CatalogItemUpsert(
                    connector_id=request.source_system,
                    store_id=value.store_id,
                    item_id=value.item_id,
                    sku_id=value.sku_id,
                    title=value.title,
                    status=value.status,
                    sale_price=value.sale_price,
                    currency=value.currency,
                    attributes=attributes,
                    source_updated_at=source_time,
                    source_id=source_id,
                ),
            )
        if adapter.domain is ReportDomain.INVENTORY:
            value = InventorySnapshotRow.model_validate(row.value)
            return self.inventory.upsert(
                tenant_id,
                InventoryBalanceUpsert(
                    connector_id=request.source_system,
                    store_id=value.store_id,
                    warehouse_id=value.warehouse_id,
                    sku_id=value.sku_id,
                    on_hand=value.on_hand,
                    reserved=value.reserved,
                    inbound=value.inbound,
                    average_daily_sales=value.average_daily_sales,
                    source_updated_at=source_time,
                    source_id=source_id,
                ),
            )
        if adapter.domain is ReportDomain.FULFILLMENT:
            value = FulfillmentSnapshotRow.model_validate(row.value)
            return self.orders.merge_logistics_snapshot(
                tenant_id,
                connector_id=request.source_system,
                store_id=value.store_id,
                order_id=value.order_id,
                logistics=LogisticsSnapshotInput(
                    carrier=value.carrier,
                    tracking_no_masked=value.tracking_no_masked,
                    status=value.logistics_status,
                    last_event=value.last_event,
                    last_event_at=self._aware(value.last_event_at, request),
                ),
                source_updated_at=source_time,
                source_id=source_id,
            )
        if adapter.domain is ReportDomain.OPERATIONS:
            value = OperationsDailyRow.model_validate(row.value)
            dataset_hash = hashlib.sha256(
                f"{request.source_system}:{request.store_id}".encode()
            ).hexdigest()[:24]
            return self.operations.upsert_record(
                tenant_id,
                OpsOperationRecordUpsert(
                    dataset_key=f"readonly.{dataset_hash}",
                    store_id=value.store_id,
                    record_date=value.metric_date,
                    channel=value.channel,
                    visitors=value.visitors,
                    orders=value.orders,
                    sales_amount=value.sales_amount,
                    ad_spend=value.ad_spend,
                    source_format="csv",
                    source_id=source_id,
                ),
            )
        if adapter.domain is ReportDomain.MARKETING:
            value = MarketingDailyRow.model_validate(row.value)
            return self.marketing.upsert_performance(
                tenant_id,
                MarketingPerformanceUpsert(
                    connector_id=request.source_system,
                    store_id=value.store_id,
                    campaign_id=value.campaign_id,
                    metric_date=value.metric_date,
                    campaign_name=value.campaign_name,
                    channel=value.channel,
                    objective=value.objective,
                    status=value.status,
                    spend=value.spend,
                    attributed_revenue=value.attributed_revenue,
                    attributed_orders=value.attributed_orders,
                    impressions=value.impressions,
                    clicks=value.clicks,
                    source_type=self._file_source_type(request),
                    source_updated_at=source_time,
                    source_id=source_id,
                ),
            )
        if adapter.domain is ReportDomain.FINANCE:
            value = SettlementStatementRow.model_validate(row.value)
            return self.finance.upsert_statement(
                tenant_id,
                SettlementStatementUpsert(
                    connector_id=request.source_system,
                    store_id=value.store_id,
                    statement_key=value.statement_key,
                    period_start=value.period_start,
                    period_end=value.period_end,
                    gross_sales=value.gross_sales,
                    refund_amount=value.refund_amount,
                    fee_amount=value.fee_amount,
                    settlement_amount=value.settlement_amount,
                    currency=value.currency,
                    source_type=self._file_source_type(request),
                    source_updated_at=source_time,
                    source_id=source_id,
                ),
            )
        raise ValueError("report_domain_writer_not_found")

    def _write_orders(
        self,
        tenant_id: str,
        rows: list[_PreparedRow],
        request: ReportImportRequest,
        *,
        digest: str,
        writes: dict[str, int],
    ) -> tuple[list[_PreparedRow], list[RowIsolationIssue], dict[str, int]]:
        grouped: dict[tuple[str, str], list[_PreparedRow]] = defaultdict(list)
        for row in rows:
            value = OrderSnapshotRow.model_validate(row.value)
            grouped[(value.store_id, value.order_id)].append(row)
        successful: list[_PreparedRow] = []
        issues: list[RowIsolationIssue] = []
        for key in sorted(grouped):
            group = grouped[key]
            values = [OrderSnapshotRow.model_validate(row.value) for row in group]
            if not self._order_group_consistent(values):
                issues.extend(
                    self._group_issues(group, "inconsistent_order_group")
                )
                continue
            first = values[0]
            source_id = self._source_id("order_snapshot", digest, first)
            try:
                result = self.orders.merge_order_lines_snapshot(
                    tenant_id,
                    OrderUpsert(
                        connector_id=request.source_system,
                        store_id=first.store_id,
                        order_id=first.order_id,
                        order_status=first.order_status,
                        payment_status=first.payment_status,
                        currency=first.currency,
                        total_amount=first.total_amount,
                        placed_at=self._aware(first.placed_at, request),
                        lines=[
                            OrderLineInput(
                                line_id=value.line_id,
                                sku_id=value.sku_id,
                                title=value.title,
                                quantity=value.quantity,
                                unit_price=value.unit_price,
                            )
                            for value in sorted(values, key=lambda item: item.line_id)
                        ],
                        source_updated_at=request.data_as_of,
                        source_id=source_id,
                    ),
                )
            except (SourceVersionError, ValueError) as exc:
                issues.extend(self._group_issues(group, self._domain_reason(exc)))
                continue
            writes[str(result["write_status"])] += 1
            successful.extend(group)
        return successful, issues, writes

    def _write_refunds(
        self,
        tenant_id: str,
        rows: list[_PreparedRow],
        request: ReportImportRequest,
        *,
        digest: str,
        writes: dict[str, int],
    ) -> tuple[list[_PreparedRow], list[RowIsolationIssue], dict[str, int]]:
        grouped: dict[tuple[str, str], list[_PreparedRow]] = defaultdict(list)
        for row in rows:
            value = RefundSnapshotRow.model_validate(row.value)
            grouped[(value.store_id, value.order_id)].append(row)
        successful: list[_PreparedRow] = []
        issues: list[RowIsolationIssue] = []
        for key in sorted(grouped):
            group = grouped[key]
            values = [RefundSnapshotRow.model_validate(row.value) for row in group]
            first = values[0]
            source_id = self._source_id("refund_snapshot", digest, first)
            try:
                result = self.orders.merge_after_sale_cases(
                    tenant_id,
                    connector_id=request.source_system,
                    store_id=first.store_id,
                    order_id=first.order_id,
                    cases=[
                        AfterSaleCaseInput(
                            case_id=value.case_id,
                            case_type=value.case_type,
                            status=value.status,
                            requested_amount=value.requested_amount,
                            approved_amount=value.approved_amount,
                            reason_code=value.reason_code,
                            opened_at=self._aware(value.opened_at, request),
                            updated_at=self._aware(value.updated_at, request),
                        )
                        for value in sorted(values, key=lambda item: item.case_id)
                    ],
                    source_updated_at=request.data_as_of,
                    source_id=source_id,
                )
            except (SourceVersionError, ValueError) as exc:
                issues.extend(self._group_issues(group, self._domain_reason(exc)))
                continue
            writes[str(result["write_status"])] += 1
            successful.extend(group)
        return successful, issues, writes

    def _record_field_evidence(
        self,
        tenant_id: str,
        request: ReportImportRequest,
        manifest: dict[str, Any],
        rows: Sequence[_PreparedRow],
    ) -> None:
        if not rows:
            return
        fields = sorted(
            {
                field
                for row in rows
                for field, value in row.value.model_dump(mode="json").items()
                if field != "store_id" and value is not None
            }
        )
        state = EvidenceState(request.source_kind.value)
        for field in fields:
            self.readonly.record_field_evidence(
                tenant_id,
                FieldEvidenceInput(
                    store_id=request.store_id,
                    field_key=f"{request.report_type}.{field}",
                    scope="store",
                    evidence_state=state,
                    reason=f"{request.report_type}_imported",
                    data_as_of=request.data_as_of,
                    source_reference=request.storage_ref,
                    import_id=manifest["import_id"],
                ),
            )

    def _result(
        self,
        tenant_id: str,
        adapter: ReportAdapter,
        manifest: dict[str, Any],
        *,
        trace: dict[str, Any],
        domain_writes: dict[str, int],
        replayed: bool,
    ) -> dict[str, Any]:
        quality_status = str(manifest["quality"]["status"])
        status = {
            "passed": "passed",
            "partial": "partial",
            "failed": "failed",
        }[quality_status]
        issues = self.readonly.list_row_issues(
            tenant_id,
            import_id=str(manifest["import_id"]),
            scope=DataScope.ALL,
        )
        return {
            "report_type": adapter.report_type,
            "mapping_version": adapter.mapping_version,
            "domain": adapter.domain.value,
            "status": status,
            "replayed": replayed,
            "trace": trace,
            "domain_writes": domain_writes,
            "manifest": manifest,
            "issues": issues,
        }

    @staticmethod
    def _trace(
        request: ReportImportRequest,
        adapter: ReportAdapter,
        parsed: ParsedReport,
    ) -> dict[str, Any]:
        return {
            "grain": adapter.grain,
            "amount_unit": adapter.amount_unit,
            "source_timezone": request.source_timezone,
            "sheet_name": parsed.sheet_name,
        }

    @staticmethod
    def _raw_row_digest(row: ParsedReportRow) -> str:
        encoded = json.dumps(
            row.values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return content_digest(encoded)

    @staticmethod
    def _issue(
        row_number: int,
        raw_digest: str,
        *,
        disposition: RowDisposition,
        reason: str,
        field_keys: Sequence[str] = (),
    ) -> RowIsolationIssue:
        return RowIsolationIssue(
            row_number=row_number,
            disposition=disposition,
            reason=reason,
            field_keys=tuple(sorted(set(field_keys))),
            raw_row_digest=raw_digest,
        )

    def _domain_issue(
        self, row: _PreparedRow, exc: Exception
    ) -> RowIsolationIssue:
        return self._issue(
            row.row_number,
            row.raw_digest,
            disposition=RowDisposition.QUARANTINED,
            reason=self._domain_reason(exc),
        )

    def _group_issues(
        self, rows: Sequence[_PreparedRow], reason: str
    ) -> list[RowIsolationIssue]:
        return [
            self._issue(
                row.row_number,
                row.raw_digest,
                disposition=RowDisposition.QUARANTINED,
                reason=reason,
            )
            for row in rows
        ]

    @staticmethod
    def _domain_reason(exc: Exception) -> str:
        code = str(exc)
        if code == "stale_source_version":
            return "stale_domain_source"
        if code == "source_version_conflict":
            return "domain_source_conflict"
        if code == "order_not_found":
            return "parent_order_not_found"
        return "domain_write_rejected"

    @staticmethod
    def _validation_fields(
        exc: ValidationError, adapter: ReportAdapter
    ) -> tuple[str, ...]:
        allowed = set(adapter.policy.allowed_fields)
        fields = {
            str(item)
            for error in exc.errors(include_input=False)
            for item in error["loc"]
            if isinstance(item, str) and item in allowed
        }
        return tuple(sorted(fields))

    @staticmethod
    def _prepared_identity(row: _PreparedRow) -> tuple[str, ...]:
        payload = row.value.model_dump(mode="json")
        return tuple(str(payload[key]) for key in sorted(payload))

    @staticmethod
    def _source_id(report_type: str, digest: str, value: BaseModel) -> str:
        identity = hashlib.sha256(
            json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:24]
        return f"readonly:{report_type}:{digest[:24]}:{identity}"

    @staticmethod
    def _aware(value: datetime, request: ReportImportRequest) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=ZoneInfo(request.source_timezone))
        return value.astimezone(UTC)

    @staticmethod
    def _file_source_type(request: ReportImportRequest) -> str:
        return "virtual" if request.source_kind.value == "demo" else "file_import"

    @staticmethod
    def _order_group_consistent(values: Sequence[OrderSnapshotRow]) -> bool:
        first = values[0]
        expected = (
            first.store_id,
            first.order_id,
            first.order_status,
            first.payment_status,
            first.currency,
            first.total_amount,
            first.placed_at,
        )
        return all(
            (
                value.store_id,
                value.order_id,
                value.order_status,
                value.payment_status,
                value.currency,
                value.total_amount,
                value.placed_at,
            )
            == expected
            for value in values
        )

    @staticmethod
    def _error_code(exc: Exception) -> str:
        candidate = str(exc).split(":", 1)[0]
        if _SAFE_ERROR_CODE.fullmatch(candidate):
            return candidate
        return "report_import_failed"


__all__ = ["ReadonlyReportIngestionService"]
