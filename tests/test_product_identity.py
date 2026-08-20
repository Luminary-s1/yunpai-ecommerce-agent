from __future__ import annotations

import sqlite3
import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ecommerce_agent.business import (
    CatalogService,
    InventoryService,
    OrderService,
    OrderUpsert,
)
from ecommerce_agent.business.catalog import CatalogItemUpsert
from ecommerce_agent.business.inventory import InventoryBalanceUpsert
from ecommerce_agent.business.orders import OrderLineInput
from ecommerce_agent.database import Database
from ecommerce_agent.product_identity import (
    PRODUCT_IDENTITY_POLICY_VERSION,
    CanonicalProductCreate,
    MappingDecisionInput,
    MatchEvidence,
    MappingRevocationInput,
    ProductIdentityObservation,
    ProductIdentityService,
    ProductReconciliationRequest,
)
from ecommerce_agent.product_identity.models import (
    MappingEventType,
    ObservationDomain,
    ReconciliationStatus,
)
from ecommerce_agent.readonly_data import (
    DataScope,
    ReadonlyReportIngestionService,
    ReportFileFormat,
    ReportImportRequest,
    SourceKind,
)


NOW = datetime(2026, 8, 18, 6, 0, tzinfo=UTC)


def _service(tmp_path, name: str = "product-identity") -> tuple[Database, ProductIdentityService]:
    db = Database(tmp_path / f"{name}.sqlite3")
    db.initialize()
    return db, ProductIdentityService(db)


def _product(
    store_id: str,
    internal_part_number: str,
    merchant_code: str,
    title: str,
    source_kind: SourceKind = SourceKind.MANUAL,
) -> CanonicalProductCreate:
    return CanonicalProductCreate(
        store_id=store_id,
        internal_part_number=internal_part_number,
        merchant_code=merchant_code,
        title=title,
        source_kind=source_kind,
        source_reference="manual:product-master:v1",
    )


def _decision(
    *,
    canonical_product_id: str,
    sku_id: str = "SKU-1",
    store_id: str = "store-a",
    connector_id: str = "controlled_export",
    item_id: str | None = "ITEM-1",
    merchant_code: str | None = "MERCHANT-A",
    expected_version: int = 0,
    decision_key: str = "decision:sku-1:v1",
) -> MappingDecisionInput:
    return MappingDecisionInput(
        store_id=store_id,
        connector_id=connector_id,
        sku_id=sku_id,
        item_id=item_id,
        merchant_code=merchant_code,
        canonical_product_id=canonical_product_id,
        expected_version=expected_version,
        decision_key=decision_key,
        reason="manual_identity_verified",
        actor_ref="operator:sha256:test",
    )


def _observation(
    *,
    sku_id: str = "SKU-1",
    store_id: str = "store-a",
    connector_id: str = "controlled_export",
    item_id: str | None = "ITEM-1",
    merchant_code: str | None = "MERCHANT-A",
    title: str | None = "恒温水壶",
    source_domain: str = "catalog",
    source_reference: str = "catalog:item-1",
) -> dict[str, object]:
    return {
        "source_domain": source_domain,
        "source_reference": source_reference,
        "store_id": store_id,
        "connector_id": connector_id,
        "sku_id": sku_id,
        "item_id": item_id,
        "merchant_code": merchant_code,
        "title": title,
    }


def test_v35_is_reserved_for_wp3_and_migrates_idempotently(tmp_path) -> None:
    db, service = _service(tmp_path, "v35-migration")
    assert service.policy_version == PRODUCT_IDENTITY_POLICY_VERSION

    with db.connect() as conn:
        versions = dict(
            conn.execute(
                "SELECT version, COUNT(*) FROM schema_migrations "
                "WHERE version=35 GROUP BY version"
            ).fetchall()
        )
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        mapping_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(readonly_product_mapping_events)"
            )
        }
        row_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(readonly_product_reconciliation_rows)"
            )
        }

    db.initialize()
    assert Database.SCHEMA_VERSION >= 35
    assert versions.get(35) == 1
    assert {
        "readonly_canonical_products",
        "readonly_product_mapping_events",
        "readonly_product_reconciliation_runs",
        "readonly_product_reconciliation_rows",
    } <= tables
    assert {
        "tenant_id",
        "store_id",
        "connector_id",
        "sku_id",
        "mapping_version",
        "event_type",
        "canonical_product_id",
        "supersedes_event_id",
        "payload_hash",
        "expected_version",
    } <= mapping_columns
    assert "evidence_keys_json" in row_columns
    with db.connect() as conn:
        mapping_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='readonly_product_mapping_events'"
            ).fetchone()[0]
        )
        row_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' "
                "AND name='readonly_product_reconciliation_rows'"
            ).fetchone()[0]
        )
    assert all(f"'{item.value}'" in mapping_sql for item in MappingEventType)
    assert all(f"'{item.value}'" in row_sql for item in ReconciliationStatus)
    assert all(f"'{item.value}'" in row_sql for item in ObservationDomain)


def test_v34_database_upgrades_to_v35_without_rebuilding_existing_data(tmp_path) -> None:
    db = Database(tmp_path / "v34-to-v35.sqlite3")
    db.path.parent.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in (*range(1, 31), 32, 33, 34):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-08-18T00:00:00+00:00"),
            )
        conn.execute("CREATE TABLE legacy_v34_marker(value TEXT NOT NULL)")
        conn.execute("INSERT INTO legacy_v34_marker VALUES ('preserved')")
        conn.execute("PRAGMA user_version=34")

    db.initialize()
    db.initialize()

    with db.connect() as conn:
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=35"
        ).fetchone()[0]
        marker = conn.execute("SELECT value FROM legacy_v34_marker").fetchone()[0]
    assert migration_count == 1
    assert marker == "preserved"


def test_unknown_product_identity_policy_fails_closed_on_read(tmp_path) -> None:
    db, service = _service(tmp_path, "unknown-policy")
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO readonly_canonical_products(
                canonical_product_id, tenant_id, store_id,
                internal_part_number, merchant_code, title, normalized_title,
                source_kind, source_reference, policy_version, payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-product",
                "tenant-a",
                "store-a",
                "PART-LEGACY",
                "MERCHANT-LEGACY",
                "legacy",
                "legacy",
                "manual",
                "manual:legacy-product",
                "product-identity-unknown",
                "0" * 64,
                "2026-08-18T00:00:00+00:00",
            ),
        )

    with pytest.raises(ValueError, match="unsupported_product_identity_policy"):
        service.list_products("tenant-a", store_id="store-a")


def test_canonical_products_are_idempotent_and_tenant_store_scoped(tmp_path) -> None:
    _db, service = _service(tmp_path, "canonical-scope")

    store_a = service.register_product(
        "tenant-a", _product("store-a", "PART-001", "MERCHANT-A", "恒温水壶")
    )
    replayed = service.register_product(
        "tenant-a", _product("store-a", "PART-001", "MERCHANT-A", "恒温水壶")
    )
    store_b = service.register_product(
        "tenant-a", _product("store-b", "PART-001", "MERCHANT-A", "恒温水壶")
    )
    tenant_b = service.register_product(
        "tenant-b", _product("store-a", "PART-001", "MERCHANT-A", "恒温水壶")
    )

    assert store_a["write_status"] == "applied"
    assert replayed["write_status"] == "idempotent"
    assert replayed["canonical_product_id"] == store_a["canonical_product_id"]
    assert len(
        {
            store_a["canonical_product_id"],
            store_b["canonical_product_id"],
            tenant_b["canonical_product_id"],
        }
    ) == 3
    assert [
        item["canonical_product_id"]
        for item in service.list_products("tenant-a", store_id="store-a")
    ] == [store_a["canonical_product_id"]]
    assert service.list_products("tenant-b", store_id="store-b") == []

    with pytest.raises(ValueError, match="canonical_product_scope_mismatch"):
        service.confirm_mapping(
            "tenant-a",
            _decision(
                canonical_product_id=store_a["canonical_product_id"],
                store_id="store-b",
            ),
        )


def test_unique_candidate_is_not_silently_promoted_to_a_mapping(tmp_path) -> None:
    _db, service = _service(tmp_path, "candidate-only")
    product = service.register_product(
        "tenant-a", _product("store-a", "PART-001", "MERCHANT-A", "恒温水壶")
    )

    result = service.reconcile(
        "tenant-a",
        ProductReconciliationRequest(
            store_id="store-a",
            observations=(_observation(),),
        ),
    )

    assert result["status_counts"] == {
        "matched": 0,
        "ambiguous": 0,
        "unmapped": 1,
        "rejected": 0,
    }
    assert result["rows"][0]["reason"] == "manual_confirmation_required"
    assert result["rows"][0]["candidate_product_ids"] == [
        product["canonical_product_id"]
    ]
    assert service.mapping_history(
        "tenant-a",
        store_id="store-a",
        connector_id="controlled_export",
        sku_id="SKU-1",
    ) == []


def test_demo_canonical_products_do_not_enter_operational_candidates(tmp_path) -> None:
    _db, service = _service(tmp_path, "canonical-source-scope")
    operational = service.register_product(
        "tenant-a", _product("store-a", "PART-A", "MERCHANT-A", "恒温水壶")
    )
    demo = service.register_product(
        "tenant-a",
        _product(
            "store-a",
            "PART-DEMO",
            "MERCHANT-A",
            "恒温水壶",
            source_kind=SourceKind.DEMO,
        ),
    )
    observation = _observation()

    operational_run = service.reconcile(
        "tenant-a",
        ProductReconciliationRequest(
            store_id="store-a",
            observations=(observation,),
        ),
    )
    demo_run = service.reconcile(
        "tenant-a",
        ProductReconciliationRequest(
            store_id="store-a",
            scope=DataScope.DEMO,
            observations=(observation,),
        ),
    )
    all_run = service.reconcile(
        "tenant-a",
        ProductReconciliationRequest(
            store_id="store-a",
            scope=DataScope.ALL,
            observations=(observation,),
        ),
    )

    assert operational_run["rows"][0]["candidate_product_ids"] == [
        operational["canonical_product_id"]
    ]
    assert demo_run["rows"][0]["candidate_product_ids"] == [
        demo["canonical_product_id"]
    ]
    assert all_run["rows"][0]["terminal_status"] == "ambiguous"


def test_conflicting_merchant_and_title_signals_are_ambiguous(tmp_path) -> None:
    _db, service = _service(tmp_path, "candidate-conflict")
    product_a = service.register_product(
        "tenant-a", _product("store-a", "PART-A", "MERCHANT-A", "相同标题")
    )
    product_b = service.register_product(
        "tenant-a", _product("store-a", "PART-B", "MERCHANT-B", "相同标题")
    )

    result = service.reconcile(
        "tenant-a",
        ProductReconciliationRequest(
            store_id="store-a",
            observations=(
                _observation(
                    sku_id="SKU-X",
                    item_id="ITEM-X",
                    merchant_code="MERCHANT-B",
                    title="相同标题",
                ),
            ),
        ),
    )

    row = result["rows"][0]
    assert row["terminal_status"] == "ambiguous"
    assert row["reason"] == "conflicting_identity_signals"
    assert row["canonical_product_id"] is None
    assert row["internal_part_number"] is None
    assert row["candidate_product_ids"] == sorted(
        [product_a["canonical_product_id"], product_b["canonical_product_id"]]
    )


def test_manual_confirmation_and_reconciliation_replay_are_stable(tmp_path) -> None:
    _db, service = _service(tmp_path, "manual-confirmation")
    product = service.register_product(
        "tenant-a", _product("store-a", "PART-001", "MERCHANT-A", "恒温水壶")
    )
    decision = _decision(canonical_product_id=product["canonical_product_id"])

    confirmed = service.confirm_mapping("tenant-a", decision)
    replayed_decision = service.confirm_mapping("tenant-a", decision)
    request = ProductReconciliationRequest(
        store_id="store-a",
        observations=(_observation(),),
    )
    first = service.reconcile("tenant-a", request)
    replayed_run = service.reconcile("tenant-a", request)

    assert confirmed["write_status"] == "applied"
    assert confirmed["mapping_version"] == 1
    assert confirmed["expected_version"] == 0
    assert service.get_product(
        "tenant-a",
        store_id="store-a",
        canonical_product_id=product["canonical_product_id"],
    )["internal_part_number"] == "PART-001"
    assert service.get_latest_mapping(
        "tenant-a",
        store_id="store-a",
        connector_id="controlled_export",
        sku_id="SKU-1",
    )["event_id"] == confirmed["event_id"]
    assert replayed_decision["write_status"] == "idempotent"
    assert replayed_decision["event_id"] == confirmed["event_id"]
    assert first["rows"][0]["terminal_status"] == "matched"
    assert first["rows"][0]["internal_part_number"] == "PART-001"
    assert first["rows"][0]["evidence_keys"] == [
        MatchEvidence.CONFIRMED_MAPPING.value,
        MatchEvidence.SKU_ID_EXACT.value,
        MatchEvidence.ITEM_ID_EXACT.value,
        MatchEvidence.MERCHANT_CODE_EXACT.value,
    ]
    assert replayed_run["write_status"] == "idempotent"
    assert replayed_run["run_id"] == first["run_id"]
    assert [item["event_type"] for item in service.mapping_history(
        "tenant-a",
        store_id="store-a",
        connector_id="controlled_export",
        sku_id="SKU-1",
    )] == ["confirmed"]
    with pytest.raises(ValueError, match="mapping_decision_key_conflict"):
        service.confirm_mapping(
            "tenant-a",
            decision.model_copy(update={"expected_version": 99}),
        )


def test_optimistic_version_blocks_competing_mapping_until_explicit_adjudication(
    tmp_path,
) -> None:
    _db, service = _service(tmp_path, "mapping-optimistic-version")
    product_a = service.register_product(
        "tenant-a", _product("store-a", "PART-A", "MERCHANT-A", "恒温水壶")
    )
    product_b = service.register_product(
        "tenant-a", _product("store-a", "PART-B", "MERCHANT-B", "恒温水壶 B")
    )
    service.confirm_mapping(
        "tenant-a", _decision(canonical_product_id=product_a["canonical_product_id"])
    )
    competing = _decision(
        canonical_product_id=product_b["canonical_product_id"],
        item_id="ITEM-2",
        merchant_code="MERCHANT-B",
        decision_key="decision:sku-1:v2",
    )

    with pytest.raises(ValueError, match="mapping_version_conflict"):
        service.confirm_mapping("tenant-a", competing)
    adjudicated = service.confirm_mapping(
        "tenant-a",
        competing.model_copy(update={"expected_version": 1}),
    )

    assert adjudicated["mapping_version"] == 2
    assert adjudicated["canonical_product_id"] == product_b["canonical_product_id"]


def test_confirmed_mapping_evidence_drift_requires_new_adjudication(tmp_path) -> None:
    _db, service = _service(tmp_path, "mapping-drift")
    product = service.register_product(
        "tenant-a", _product("store-a", "PART-001", "MERCHANT-A", "恒温水壶")
    )
    service.confirm_mapping(
        "tenant-a", _decision(canonical_product_id=product["canonical_product_id"])
    )

    result = service.reconcile(
        "tenant-a",
        ProductReconciliationRequest(
            store_id="store-a",
            observations=(
                _observation(item_id="ITEM-CHANGED", merchant_code="MERCHANT-B"),
            ),
        ),
    )

    assert result["rows"][0]["terminal_status"] == "ambiguous"
    assert result["rows"][0]["reason"] == "confirmed_mapping_evidence_conflict"
    assert result["rows"][0]["canonical_product_id"] is None


def test_revocation_changes_new_runs_but_preserves_mapping_and_run_history(tmp_path) -> None:
    _db, service = _service(tmp_path, "mapping-revocation")
    product = service.register_product(
        "tenant-a", _product("store-a", "PART-001", "MERCHANT-A", "恒温水壶")
    )
    service.confirm_mapping(
        "tenant-a", _decision(canonical_product_id=product["canonical_product_id"])
    )
    request = ProductReconciliationRequest(
        store_id="store-a",
        observations=(_observation(),),
    )
    before = service.reconcile("tenant-a", request)
    revocation = MappingRevocationInput(
        store_id="store-a",
        connector_id="controlled_export",
        sku_id="SKU-1",
        expected_version=1,
        decision_key="decision:sku-1:revoke:v2",
        reason="mapping_evidence_invalidated",
        actor_ref="operator:sha256:test",
    )

    revoked = service.revoke_mapping("tenant-a", revocation)
    replayed = service.revoke_mapping("tenant-a", revocation)
    after = service.reconcile("tenant-a", request)
    historical = service.get_reconciliation("tenant-a", before["run_id"])

    assert revoked["mapping_version"] == 2
    assert revoked["expected_version"] == 1
    assert revoked["event_type"] == "revoked"
    assert replayed["write_status"] == "idempotent"
    assert service.get_latest_mapping(
        "tenant-a",
        store_id="store-a",
        connector_id="controlled_export",
        sku_id="SKU-1",
    )["event_type"] == "revoked"
    assert after["run_id"] != before["run_id"]
    assert after["rows"][0]["terminal_status"] == "unmapped"
    assert after["rows"][0]["reason"] == "mapping_revoked"
    assert historical["rows"][0]["terminal_status"] == "matched"
    assert [item["event_type"] for item in service.mapping_history(
        "tenant-a",
        store_id="store-a",
        connector_id="controlled_export",
        sku_id="SKU-1",
    )] == ["confirmed", "revoked"]


def test_reconciliation_covers_every_input_row_with_one_terminal_status(tmp_path) -> None:
    _db, service = _service(tmp_path, "full-row-coverage")
    product_a = service.register_product(
        "tenant-a", _product("store-a", "PART-A", "MERCHANT-A", "相同标题")
    )
    service.register_product(
        "tenant-a", _product("store-a", "PART-B", "MERCHANT-B", "相同标题")
    )
    service.confirm_mapping(
        "tenant-a",
        _decision(canonical_product_id=product_a["canonical_product_id"]),
    )
    malformed = _observation(sku_id="SKU-REJECTED")
    malformed.pop("connector_id")

    result = service.reconcile(
        "tenant-a",
        ProductReconciliationRequest(
            store_id="store-a",
            observations=(
                _observation(),
                _observation(
                    sku_id="SKU-AMBIGUOUS",
                    item_id="ITEM-2",
                    merchant_code="MERCHANT-B",
                    title="相同标题",
                    source_reference="catalog:item-2",
                ),
                _observation(
                    sku_id="SKU-UNMAPPED",
                    item_id="ITEM-3",
                    merchant_code=None,
                    title="无候选商品",
                    source_reference="catalog:item-3",
                ),
                malformed,
                "not-a-row",
            ),
        ),
    )

    assert result["total_rows"] == 5
    assert result["status_counts"] == {
        "matched": 1,
        "ambiguous": 1,
        "unmapped": 1,
        "rejected": 2,
    }
    assert [row["row_number"] for row in result["rows"]] == [1, 2, 3, 4, 5]
    assert [row["terminal_status"] for row in result["rows"]] == [
        "matched",
        "ambiguous",
        "unmapped",
        "rejected",
        "rejected",
    ]
    assert sum(result["status_counts"].values()) == result["total_rows"]
    assert result["rows"][4]["evidence_keys"] == [
        MatchEvidence.INVALID_OBSERVATION.value
    ]


def test_observation_source_reference_rejects_uncontrolled_text() -> None:
    with pytest.raises(ValueError, match="invalid_observation_source_reference"):
        ProductIdentityObservation.model_validate(
            _observation(source_reference="../客户手机号13800138000.txt")
        )


def test_cross_store_observation_is_rejected_without_leaking_another_scope(tmp_path) -> None:
    _db, service = _service(tmp_path, "cross-store-observation")
    service.register_product(
        "tenant-a", _product("store-b", "PART-001", "MERCHANT-A", "恒温水壶")
    )

    result = service.reconcile(
        "tenant-a",
        ProductReconciliationRequest(
            store_id="store-a",
            observations=(_observation(store_id="store-b"),),
        ),
    )

    assert result["rows"][0]["terminal_status"] == "rejected"
    assert result["rows"][0]["reason"] == "cross_store_observation"
    assert result["rows"][0]["candidate_product_ids"] == []


def test_domain_reconciliation_uses_catalog_order_and_inventory_public_truth(tmp_path) -> None:
    db, service = _service(tmp_path, "domain-reconciliation")
    CatalogService(db).upsert(
        "tenant-a",
        CatalogItemUpsert(
            connector_id="controlled_export",
            store_id="store-a",
            item_id="ITEM-1",
            sku_id="SKU-1",
            title="恒温水壶",
            status="active",
            sale_price=Decimal("129.00"),
            currency="CNY",
            attributes={"merchant_code": "MERCHANT-A"},
            source_updated_at=NOW,
            source_id="manual:catalog:test",
        ),
    )
    InventoryService(db).upsert(
        "tenant-a",
        InventoryBalanceUpsert(
            connector_id="controlled_export",
            store_id="store-a",
            warehouse_id="WH-1",
            sku_id="SKU-1",
            on_hand=Decimal("8"),
            source_updated_at=NOW,
            source_id="manual:inventory:test",
        ),
    )
    OrderService(db).upsert(
        "tenant-a",
        OrderUpsert(
            connector_id="controlled_export",
            store_id="store-a",
            order_id="ORDER-1",
            order_status="paid",
            payment_status="paid",
            currency="CNY",
            total_amount=Decimal("129.00"),
            placed_at=NOW,
            lines=[
                OrderLineInput(
                    line_id="LINE-1",
                    sku_id="SKU-1",
                    title="恒温水壶",
                    quantity=1,
                    unit_price=Decimal("129.00"),
                )
            ],
            source_updated_at=NOW,
            source_id="manual:orders:test",
        ),
    )
    product = service.register_product(
        "tenant-a", _product("store-a", "PART-001", "MERCHANT-A", "恒温水壶")
    )
    service.confirm_mapping(
        "tenant-a", _decision(canonical_product_id=product["canonical_product_id"])
    )

    result = service.reconcile_domain("tenant-a", store_id="store-a")

    assert result["total_rows"] == 3
    assert result["status_counts"] == {
        "matched": 3,
        "ambiguous": 0,
        "unmapped": 0,
        "rejected": 0,
    }
    assert {row["source_domain"] for row in result["rows"]} == {
        "catalog",
        "inventory",
        "order",
    }
    assert {row["internal_part_number"] for row in result["rows"]} == {
        "PART-001"
    }


def test_domain_reconciliation_preserves_report_source_scope(tmp_path) -> None:
    db, service = _service(tmp_path, "domain-source-scope")
    content = io.StringIO(newline="")
    writer = csv.DictWriter(
        content,
        fieldnames=[
            "store_id",
            "item_id",
            "sku_id",
            "title",
            "status",
            "sale_price",
            "currency",
            "merchant_code",
        ],
    )
    writer.writeheader()
    actual_row = {
        "store_id": "store-a",
        "item_id": "ITEM-ACTUAL",
        "sku_id": "SKU-ACTUAL",
        "title": "真实商品",
        "status": "active",
        "sale_price": "10.00",
        "currency": "CNY",
        "merchant_code": "MERCHANT-ACTUAL",
    }
    demo_row = {
        "store_id": "store-a",
        "item_id": "ITEM-DEMO",
        "sku_id": "SKU-DEMO",
        "title": "演示商品",
        "status": "active",
        "sale_price": "11.00",
        "currency": "CNY",
        "merchant_code": "MERCHANT-DEMO",
    }
    writer.writerow(actual_row)
    actual_raw = content.getvalue().encode()
    content = io.StringIO(newline="")
    writer = csv.DictWriter(content, fieldnames=writer.fieldnames)
    writer.writeheader()
    writer.writerow(demo_row)
    demo_raw = content.getvalue().encode()
    ingestion = ReadonlyReportIngestionService(db)

    def import_request(source_kind: SourceKind, suffix: str) -> ReportImportRequest:
        exported_at = datetime(2026, 8, 18, 4, 0, tzinfo=UTC)
        return ReportImportRequest(
            store_id="store-a",
            source_kind=source_kind,
            source_system="controlled_export",
            report_type="catalog_snapshot",
            mapping_version="generic-cn-v1",
            report_period=f"2026-08-18-{suffix}",
            exported_at=exported_at,
            data_as_of=exported_at,
            file_format=ReportFileFormat.CSV,
            storage_ref=f"objects/readonly-imports/domain-scope-{suffix}.csv",
            source_timezone="Asia/Shanghai",
        )

    actual = ingestion.ingest(
        "tenant-a", import_request(SourceKind.ACTUAL, "actual"), actual_raw
    )
    demo = ingestion.ingest(
        "tenant-a", import_request(SourceKind.DEMO, "demo"), demo_raw
    )
    assert actual["status"] == "passed"
    assert demo["status"] == "passed"

    actual_product = service.register_product(
        "tenant-a",
        _product("store-a", "PART-ACTUAL", "MERCHANT-ACTUAL", "真实商品"),
    )
    demo_product = service.register_product(
        "tenant-a",
        _product(
            "store-a",
            "PART-DEMO",
            "MERCHANT-DEMO",
            "演示商品",
            source_kind=SourceKind.DEMO,
        ),
    )
    service.confirm_mapping(
        "tenant-a",
        _decision(
            canonical_product_id=actual_product["canonical_product_id"],
            sku_id="SKU-ACTUAL",
            item_id="ITEM-ACTUAL",
            merchant_code="MERCHANT-ACTUAL",
            decision_key="decision:actual:v1",
        ),
    )
    service.confirm_mapping(
        "tenant-a",
        _decision(
            canonical_product_id=demo_product["canonical_product_id"],
            sku_id="SKU-DEMO",
            item_id="ITEM-DEMO",
            merchant_code="MERCHANT-DEMO",
            decision_key="decision:demo:v1",
        ),
    )

    operational = service.reconcile_domain("tenant-a", store_id="store-a")
    demo_view = service.reconcile_domain(
        "tenant-a", store_id="store-a", scope=DataScope.DEMO
    )
    all_view = service.reconcile_domain(
        "tenant-a", store_id="store-a", scope=DataScope.ALL
    )

    assert operational["total_rows"] == 1
    assert {row["sku_id"] for row in operational["rows"]} == {"SKU-ACTUAL"}
    assert demo_view["total_rows"] == 1
    assert {row["sku_id"] for row in demo_view["rows"]} == {"SKU-DEMO"}
    assert all_view["total_rows"] == 2


def test_wp3_history_tables_are_append_only(tmp_path) -> None:
    db, service = _service(tmp_path, "immutable-history")
    product = service.register_product(
        "tenant-a", _product("store-a", "PART-001", "MERCHANT-A", "恒温水壶")
    )
    service.confirm_mapping(
        "tenant-a", _decision(canonical_product_id=product["canonical_product_id"])
    )
    run = service.reconcile(
        "tenant-a",
        ProductReconciliationRequest(
            store_id="store-a",
            observations=(_observation(),),
        ),
    )

    with db.connect() as conn:
        for table in (
            "readonly_canonical_products",
            "readonly_product_mapping_events",
            "readonly_product_reconciliation_runs",
            "readonly_product_reconciliation_rows",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"UPDATE {table} SET created_at=created_at")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(f"DELETE FROM {table}")

    assert service.get_reconciliation("tenant-a", run["run_id"])["run_id"] == run[
        "run_id"
    ]
