from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ..database import Database
from ..product_identity import (
    CanonicalProductCreate,
    MappingDecisionInput,
    ProductIdentityService,
)
from ..readonly_data import (
    DataScope,
    ReadonlyDataService,
    ReadonlyReportIngestionService,
    ReportFileFormat,
    ReportImportRequest,
    SourceKind,
)
from .models import READONLY_DEMO_FIXTURE_ID, ReadonlyDemoLoadRequest
from .service import ReadonlyReadinessService


class ReadonlyDemoService:
    """Explicitly load one sanitized local fixture through WP2 public services."""

    fixture_path = Path(__file__).with_name("fixtures") / "m7r_readonly_demo_v1.json"

    def __init__(self, db: Database):
        self.db = db
        self.readonly = ReadonlyDataService(db)
        self.ingestion = ReadonlyReportIngestionService(db)
        self.identity = ProductIdentityService(db)
        self.readiness = ReadonlyReadinessService(db)

    def load(
        self,
        tenant_id: str,
        request: ReadonlyDemoLoadRequest,
        *,
        actor: str,
    ) -> dict[str, Any]:
        fixture = self._load_fixture()
        if request.fixture_id != fixture["fixture_id"]:
            raise ValueError("readonly_demo_fixture_not_found")
        operational_before = {
            str(item["import_id"])
            for item in self.readonly.list_imports(
                tenant_id,
                store_id=request.store_id,
                scope=DataScope.OPERATIONAL,
                limit=1000,
            )
        }
        reports: list[dict[str, Any]] = []
        for definition in fixture["reports"]:
            content = self._csv_content(
                definition["rows"], store_id=request.store_id
            )
            store_digest = hashlib.sha256(request.store_id.encode()).hexdigest()[:16]
            report_type = str(definition["report_type"])
            reports.append(
                self.ingestion.ingest(
                    tenant_id,
                    ReportImportRequest(
                        store_id=request.store_id,
                        source_kind=SourceKind.DEMO,
                        source_system=str(fixture["source_system"]),
                        report_type=report_type,
                        mapping_version="generic-cn-v1",
                        report_period=str(definition["report_period"]),
                        exported_at=datetime.fromisoformat(str(fixture["exported_at"])),
                        data_as_of=datetime.fromisoformat(
                            str(definition.get("data_as_of", fixture["data_as_of"]))
                        ),
                        file_format=ReportFileFormat.CSV,
                        storage_ref=(
                            "objects/readonly-imports/"
                            f"{fixture['fixture_id']}/{store_digest}/{report_type}.csv"
                        ),
                        source_timezone=str(fixture["source_timezone"]),
                    ),
                    content,
                )
            )
        sanitized_fixture = all(
            report["status"] == "passed" and not report["issues"]
            for report in reports
        )
        if not sanitized_fixture:
            raise ValueError("readonly_demo_fixture_verification_failed")

        product_definition = dict(fixture["canonical_product"])
        product = self.identity.register_product(
            tenant_id,
            CanonicalProductCreate(
                store_id=request.store_id,
                internal_part_number=str(product_definition["internal_part_number"]),
                merchant_code=str(product_definition["merchant_code"]),
                title=str(product_definition["title"]),
                source_kind=SourceKind.DEMO,
                source_reference=(
                    f"{fixture['fixture_id']}:{fixture['fixture_version']}"
                ),
            ),
        )
        catalog_manifest = next(
            report["manifest"]
            for report in reports
            if report["report_type"] == "catalog_snapshot"
        )
        decision_suffix = hashlib.sha256(request.store_id.encode()).hexdigest()[:24]
        mapping = self.identity.confirm_mapping(
            tenant_id,
            MappingDecisionInput(
                store_id=request.store_id,
                connector_id=str(fixture["source_system"]),
                sku_id=str(product_definition["sku_id"]),
                item_id=str(product_definition["item_id"]),
                merchant_code=str(product_definition["merchant_code"]),
                canonical_product_id=str(product["canonical_product_id"]),
                expected_version=0,
                decision_key=f"readonly-demo-{decision_suffix}",
                reason="demo_fixture_confirmed",
                actor_ref="readonly-demo-loader",
                source_import_id=str(catalog_manifest["import_id"]),
            ),
        )
        reconciliation = self.identity.reconcile_domain(
            tenant_id,
            store_id=request.store_id,
            scope=DataScope.DEMO,
        )
        operational_after = {
            str(item["import_id"])
            for item in self.readonly.list_imports(
                tenant_id,
                store_id=request.store_id,
                scope=DataScope.OPERATIONAL,
                limit=1000,
            )
        }
        readiness = self.readiness.project(
            tenant_id,
            store_id=request.store_id,
            scope=DataScope.DEMO,
        )
        applied = sum(
            report["manifest"]["write_status"] == "applied" for report in reports
        )
        idempotent = sum(
            report["manifest"]["write_status"] == "idempotent"
            for report in reports
        )
        return {
            "fixture_id": str(fixture["fixture_id"]),
            "fixture_version": str(fixture["fixture_version"]),
            "store_id": request.store_id,
            "source_kind": SourceKind.DEMO.value,
            "actor": actor,
            "production_claim": False,
            "summary": {
                "reports_total": len(reports),
                "reports_applied": applied,
                "reports_idempotent": idempotent,
                "reports_failed": len(reports) - applied - idempotent,
            },
            "reports": [
                {
                    "report_type": report["report_type"],
                    "domain": report["domain"],
                    "status": report["status"],
                    "write_status": report["manifest"]["write_status"],
                    "import_id": report["manifest"]["import_id"],
                    "quality": report["manifest"]["quality"],
                }
                for report in reports
            ],
            "product": product,
            "mapping": mapping,
            "reconciliation": {
                key: value
                for key, value in reconciliation.items()
                if key != "rows"
            },
            "readiness": readiness,
            "verification": {
                "sanitized_fixture": sanitized_fixture,
                "operational_scope_unchanged": (
                    operational_before == operational_after
                ),
                "model_used": False,
                "platform_write_performed": False,
                "missing_gap_keys": [
                    item["field_key"] for item in readiness["gaps"] if item["open"]
                ],
            },
        }

    @classmethod
    def fixture_summary(cls) -> dict[str, Any]:
        fixture = cls._load_fixture()
        return {
            "fixture_id": fixture["fixture_id"],
            "fixture_version": fixture["fixture_version"],
            "source_kind": SourceKind.DEMO.value,
            "report_types": [
                item["report_type"] for item in fixture["reports"]
            ],
            "sanitized": True,
            "production_claim": False,
        }

    @classmethod
    def _load_fixture(cls) -> dict[str, Any]:
        fixture = json.loads(cls.fixture_path.read_text(encoding="utf-8"))
        if fixture.get("fixture_id") != READONLY_DEMO_FIXTURE_ID:
            raise ValueError("readonly_demo_fixture_contract_invalid")
        reports = fixture.get("reports")
        if not isinstance(reports, list) or len(reports) != 8:
            raise ValueError("readonly_demo_fixture_contract_invalid")
        return fixture

    @staticmethod
    def _csv_content(rows: Any, *, store_id: str) -> bytes:
        if not isinstance(rows, list) or not rows or not all(
            isinstance(row, Mapping) for row in rows
        ):
            raise ValueError("readonly_demo_fixture_rows_invalid")
        materialized = [
            {
                str(key): store_id if value == "$store_id" else value
                for key, value in row.items()
            }
            for row in rows
        ]
        headers = list(materialized[0])
        if any(list(row) != headers for row in materialized):
            raise ValueError("readonly_demo_fixture_headers_invalid")
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=headers, lineterminator="\n")
        writer.writeheader()
        writer.writerows(materialized)
        return output.getvalue().encode("utf-8")


__all__ = ["ReadonlyDemoService"]
