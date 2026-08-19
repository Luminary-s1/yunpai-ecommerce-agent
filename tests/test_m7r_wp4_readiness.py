from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.product_identity import MappingRevocationInput
from ecommerce_agent.readonly_data import (
    DataScope,
    EvidenceState,
    FieldEvidenceInput,
    ImportManifestInput,
    ImportReference,
    REPORT_ADAPTERS,
    ReferenceKind,
    RowDisposition,
    RowIsolationIssue,
    SourceKind,
)
from ecommerce_agent.readonly_readiness import (
    READINESS_GAP_REQUIREMENTS,
    READINESS_REPORT_POLICIES,
    ReadonlyDemoLoadRequest,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}
STORE_ID = "readonly-demo-store"
FIXTURE_ID = "m7r-readonly-demo-v1"
FIXTURE_AS_OF = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _table_counts(service: AgentService, *, prefix: str = "readonly_") -> dict[str, int]:
    with service.db.connect() as conn:
        tables = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name LIKE ?
            ORDER BY name
            """,
            (f"{prefix}%",),
        ).fetchall()
        return {
            str(row["name"]): int(
                conn.execute(f'SELECT COUNT(*) FROM "{row["name"]}"').fetchone()[0]
            )
            for row in tables
        }


def _load_demo(service: AgentService) -> dict:
    return service.readonly_demo.load(
        "tenant-test",
        ReadonlyDemoLoadRequest(
            fixture_id=FIXTURE_ID,
            store_id=STORE_ID,
            confirm_demo=True,
        ),
        actor="admin-test",
    )


def test_wp4_policy_registry_covers_adapters_without_copying_adapter_metadata() -> None:
    adapter_types = {adapter.report_type for adapter in REPORT_ADAPTERS.list()}

    assert set(READINESS_REPORT_POLICIES) == adapter_types
    assert {
        policy.report_type for policy in READINESS_REPORT_POLICIES.values()
    } == adapter_types
    assert all(policy.max_age_hours > 0 for policy in READINESS_REPORT_POLICIES.values())
    assert set(READINESS_GAP_REQUIREMENTS) == {
        "purchase_cost",
        "purchase_order",
        "transport_cycle",
        "refurbishment_cost",
    }


def test_empty_readiness_projection_is_traceable_and_read_only(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        before = _table_counts(service)
        projection = service.readonly_readiness.project(
            "tenant-test",
            store_id="empty-store",
            scope=DataScope.OPERATIONAL,
            as_of=FIXTURE_AS_OF,
        )

        assert _table_counts(service) == before
        assert projection["policy_version"] == "readonly-readiness-v1"
        assert projection["scope"] == "operational"
        assert projection["summary"] == {
            "status": "missing",
            "domain_counts": {"ready": 0, "attention": 0, "missing": 8},
            "available_domains": 0,
            "total_domains": 8,
            "open_gap_count": 4,
        }
        assert {item["report_type"] for item in projection["domains"]} == {
            adapter.report_type for adapter in REPORT_ADAPTERS.list()
        }
        assert all(item["status"] == "missing" for item in projection["domains"])
        assert all(item["source"]["kind"] == "missing" for item in projection["domains"])
        assert all(item["trace"]["import_ids"] == [] for item in projection["domains"])
        assert {item["field_key"] for item in projection["gaps"]} == set(
            READINESS_GAP_REQUIREMENTS
        )
        assert all(item["evidence_state"] == "missing" for item in projection["gaps"])
    finally:
        service.close()


def test_demo_load_is_sanitized_idempotent_and_scope_isolated(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        first = _load_demo(service)
        after_first = _table_counts(service)
        replay = _load_demo(service)

        assert first["fixture_id"] == FIXTURE_ID
        assert first["source_kind"] == "demo"
        assert first["production_claim"] is False
        assert first["verification"]["sanitized_fixture"] is True
        assert first["verification"]["operational_scope_unchanged"] is True
        assert first["summary"] == {
            "reports_total": 8,
            "reports_applied": 8,
            "reports_idempotent": 0,
            "reports_failed": 0,
        }
        assert replay["summary"] == {
            "reports_total": 8,
            "reports_applied": 0,
            "reports_idempotent": 8,
            "reports_failed": 0,
        }
        assert replay["product"]["write_status"] == "idempotent"
        assert replay["mapping"]["write_status"] == "idempotent"
        assert replay["reconciliation"]["write_status"] == "idempotent"
        assert _table_counts(service) == after_first

        operational = service.readonly_readiness.project(
            "tenant-test",
            store_id=STORE_ID,
            scope=DataScope.OPERATIONAL,
            as_of=FIXTURE_AS_OF,
        )
        demo = service.readonly_readiness.project(
            "tenant-test",
            store_id=STORE_ID,
            scope=DataScope.DEMO,
            as_of=FIXTURE_AS_OF + timedelta(hours=1),
        )

        assert operational["summary"]["available_domains"] == 0
        assert operational["summary"]["status"] == "missing"
        assert demo["summary"]["available_domains"] == 8
        assert demo["summary"]["status"] == "attention"
        assert all(item["source"]["kind"] == "demo" for item in demo["domains"])
        assert all(item["freshness"]["status"] == "fresh" for item in demo["domains"])
        assert all(item["trace"]["import_ids"] for item in demo["domains"])
        assert {item["field_key"] for item in demo["gaps"]} == set(
            READINESS_GAP_REQUIREMENTS
        )
        assert demo["product_identity"]["status"] == "matched"
        assert demo["product_identity"]["status_counts"] == {
            "matched": 3,
            "ambiguous": 0,
            "unmapped": 0,
            "rejected": 0,
        }

        future = service.readonly_readiness.project(
            "tenant-test",
            store_id=STORE_ID,
            scope=DataScope.DEMO,
            as_of=datetime(2026, 8, 18, 23, 0, tzinfo=UTC),
        )
        assert all(
            item["freshness"]["status"] == "future"
            for item in future["domains"]
        )
        assert future["summary"]["status"] == "attention"

        stale = service.readonly_readiness.project(
            "tenant-test",
            store_id=STORE_ID,
            scope=DataScope.DEMO,
            as_of=FIXTURE_AS_OF + timedelta(days=90),
        )
        stale_by_type = {item["report_type"]: item for item in stale["domains"]}
        assert stale_by_type["inventory_snapshot"]["freshness"]["status"] == "stale"
        assert stale_by_type["settlement_statement"]["freshness"]["status"] == "stale"
    finally:
        service.close()


def test_concurrent_demo_replays_apply_each_immutable_fact_once(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        def load() -> dict:
            return _load_demo(service)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: load(), range(2)))

        assert sum(item["summary"]["reports_applied"] for item in results) == 8
        assert sum(item["summary"]["reports_idempotent"] for item in results) == 8
        assert {item["product"]["write_status"] for item in results} == {
            "applied",
            "idempotent",
        }
        assert {item["mapping"]["write_status"] for item in results} == {
            "applied",
            "idempotent",
        }
        assert {item["reconciliation"]["write_status"] for item in results} == {
            "applied",
            "idempotent",
        }
        with service.db.connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM readonly_import_manifests"
            ).fetchone()[0] == 8
            assert conn.execute(
                "SELECT COUNT(*) FROM readonly_product_mapping_events"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM readonly_product_reconciliation_runs"
            ).fetchone()[0] == 1
    finally:
        service.close()


def test_mapping_history_never_marks_superseded_confirmation_active(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _load_demo(service)
        service.product_identity.revoke_mapping(
            "tenant-test",
            MappingRevocationInput(
                store_id=STORE_ID,
                connector_id="readonly_demo",
                sku_id="DEMO-SKU-001",
                expected_version=1,
                decision_key="readonly-demo-revoke-v2",
                reason="demo_mapping_invalidated",
                actor_ref="operator:wp4-test",
            ),
        )

        history = service.product_identity.list_mappings(
            "tenant-test",
            store_id=STORE_ID,
            scope=DataScope.DEMO,
            latest_only=False,
        )
        latest = service.product_identity.list_mappings(
            "tenant-test",
            store_id=STORE_ID,
            scope=DataScope.DEMO,
        )

        assert [item["event_type"] for item in history] == [
            "revoked",
            "confirmed",
        ]
        assert [item["active"] for item in history] == [False, False]
        assert len(latest) == 1
        assert latest[0]["event_type"] == "revoked"
        assert latest[0]["active"] is False
    finally:
        service.close()


def test_demo_readiness_is_tenant_and_store_scoped(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        _load_demo(service)
        same_store_other_tenant = service.readonly_readiness.project(
            "tenant-other",
            store_id=STORE_ID,
            scope=DataScope.DEMO,
            as_of=FIXTURE_AS_OF,
        )
        same_tenant_other_store = service.readonly_readiness.project(
            "tenant-test",
            store_id="other-store",
            scope=DataScope.DEMO,
            as_of=FIXTURE_AS_OF,
        )

        assert same_store_other_tenant["summary"]["available_domains"] == 0
        assert same_tenant_other_store["summary"]["available_domains"] == 0
        assert same_store_other_tenant["product_identity"]["run_id"] is None
        assert same_tenant_other_store["product_identity"]["run_id"] is None
    finally:
        service.close()


def test_manifest_quality_and_field_evidence_drive_projection(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        digest = "a" * 64
        manifest = service.readonly_data.record_import(
            "tenant-test",
            ImportManifestInput(
                store_id="evidence-store",
                source_kind=SourceKind.ACTUAL,
                source_system="controlled_export",
                report_type="catalog_snapshot",
                report_period="2026-08-19 snapshot",
                exported_at=FIXTURE_AS_OF,
                schema_fingerprint="b" * 64,
                content_digest=digest,
                mapping_version="generic-cn-v1",
                parsed_rows=2,
                data_as_of=FIXTURE_AS_OF - timedelta(hours=1),
                references=(
                    ImportReference(
                        kind=ReferenceKind.RAW_FILE,
                        reference="objects/readonly-imports/evidence/catalog.csv",
                        content_digest=digest,
                    ),
                ),
            ),
            row_issues=(
                RowIsolationIssue(
                    row_number=2,
                    disposition=RowDisposition.QUARANTINED,
                    reason="domain_source_conflict",
                    field_keys=("sku_id",),
                    raw_row_digest="c" * 64,
                ),
            ),
        )
        service.readonly_data.record_field_evidence(
            "tenant-test",
            FieldEvidenceInput(
                store_id="evidence-store",
                field_key="purchase_cost",
                scope="store",
                evidence_state=EvidenceState.ACTUAL,
                reason="purchase_cost_imported",
                data_as_of=FIXTURE_AS_OF - timedelta(hours=1),
                import_id=manifest["import_id"],
            ),
        )

        projection = service.readonly_readiness.project(
            "tenant-test",
            store_id="evidence-store",
            scope=DataScope.OPERATIONAL,
            as_of=FIXTURE_AS_OF,
        )
        catalog = next(
            item
            for item in projection["domains"]
            if item["report_type"] == "catalog_snapshot"
        )
        gaps = {item["field_key"]: item for item in projection["gaps"]}

        assert catalog["status"] == "attention"
        assert catalog["quality"]["status"] == "partial"
        assert catalog["quality"]["accepted_rows"] == 1
        assert catalog["quality"]["quarantined_rows"] == 1
        assert catalog["quality"]["import_id"] == manifest["import_id"]
        assert catalog["trace"]["import_ids"] == [manifest["import_id"]]
        assert gaps["purchase_cost"]["open"] is False
        assert gaps["purchase_cost"]["evidence_state"] == "actual"
        assert projection["summary"]["open_gap_count"] == 3
    finally:
        service.close()


def test_readonly_data_api_requires_admin_and_gets_never_mutate(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get(
            f"/v1/readonly-data/readiness?store_id={STORE_ID}"
        ).status_code == 401
        missing_confirmation = client.post(
            "/v1/readonly-data/demo/load",
            headers=ADMIN_HEADERS,
            json={"fixture_id": FIXTURE_ID, "store_id": STORE_ID},
        )
        assert missing_confirmation.status_code == 422

        loaded = client.post(
            "/v1/readonly-data/demo/load",
            headers=ADMIN_HEADERS,
            json={
                "fixture_id": FIXTURE_ID,
                "store_id": STORE_ID,
                "confirm_demo": True,
            },
        )
        assert loaded.status_code == 200
        with app.state.agent.db.connect() as conn:
            audit = conn.execute(
                """
                SELECT event_type, actor, subject_id, tenant_id, detail_json
                FROM audit_log
                WHERE event_type = 'readonly_data.demo.loaded'
                """
            ).fetchone()
        assert audit is not None
        assert audit["event_type"] == "readonly_data.demo.loaded"
        assert audit["actor"] == "admin-test"
        assert audit["subject_id"] == STORE_ID
        assert audit["tenant_id"] == "tenant-test"
        assert json.loads(audit["detail_json"]) == {
            "fixture_id": FIXTURE_ID,
            "reports_applied": 8,
            "reports_idempotent": 0,
        }
        before_gets = _table_counts(app.state.agent)

        readiness = client.get(
            f"/v1/readonly-data/readiness?store_id={STORE_ID}&scope=demo",
            headers=ADMIN_HEADERS,
        )
        imports = client.get(
            f"/v1/readonly-data/imports?store_id={STORE_ID}&scope=demo",
            headers=ADMIN_HEADERS,
        )
        issues = client.get(
            f"/v1/readonly-data/row-issues?store_id={STORE_ID}&scope=demo",
            headers=ADMIN_HEADERS,
        )
        evidence = client.get(
            f"/v1/readonly-data/field-evidence?store_id={STORE_ID}&scope=demo",
            headers=ADMIN_HEADERS,
        )
        mappings = client.get(
            f"/v1/readonly-data/mappings?store_id={STORE_ID}&scope=demo",
            headers=ADMIN_HEADERS,
        )
        reconciliations = client.get(
            f"/v1/readonly-data/reconciliations?store_id={STORE_ID}&scope=demo",
            headers=ADMIN_HEADERS,
        )

        assert all(
            response.status_code == 200
            for response in (
                readiness,
                imports,
                issues,
                evidence,
                mappings,
                reconciliations,
            )
        )
        assert len(imports.json()["items"]) == 8
        assert issues.json()["items"] == []
        assert evidence.json()["items"]
        assert len(mappings.json()["items"]) == 1
        assert mappings.json()["items"][0]["active"] is True
        assert len(reconciliations.json()["items"]) == 1
        assert readiness.json()["product_identity"]["run_id"] == (
            reconciliations.json()["items"][0]["run_id"]
        )
        assert _table_counts(app.state.agent) == before_gets

        operational = client.get(
            f"/v1/readonly-data/imports?store_id={STORE_ID}&scope=operational",
            headers=ADMIN_HEADERS,
        )
        other_store = client.get(
            "/v1/readonly-data/imports?store_id=other-store&scope=all",
            headers=ADMIN_HEADERS,
        )
        assert operational.json()["items"] == []
        assert other_store.json()["items"] == []


class _ReadonlyConsoleStructure(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.nav_views: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "button" and values.get("data-view"):
            self.nav_views.add(values["data-view"])


def test_admin_console_has_readiness_view_and_only_explicit_demo_action(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    before = _table_counts(app.state.agent)
    with TestClient(app) as client:
        page = client.get("/admin")
    structure = _ReadonlyConsoleStructure()
    structure.feed(page.text)

    assert page.status_code == 200
    assert _table_counts(app.state.agent) == before
    assert "readonly-data" in structure.nav_views
    assert {
        "readonlyStore",
        "readonlyScope",
        "loadReadonlyReadiness",
        "loadReadonlyDemo",
        "readonlySummary",
        "readonlyDomainRows",
        "readonlyGapRows",
        "readonlyImportRows",
        "readonlyIssueRows",
        "readonlyMappingRows",
        "readonlyEvidenceRows",
    } <= structure.ids
    assert "loadReadonlyData" in page.text
    assert "renderReadonlyData" in page.text
    assert "/v1/readonly-data/readiness" in page.text
    assert "/v1/readonly-data/demo/load" in page.text
    assert "confirm_demo:true" in page.text
    assert "loadReadonlyDemo').addEventListener('click'" in page.text
