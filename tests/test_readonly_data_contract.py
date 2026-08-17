from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import ecommerce_agent.readonly_data.service as readonly_service_module
from ecommerce_agent.business.source_versioning import SourceVersionError
from ecommerce_agent.database import Database
from ecommerce_agent.readonly_data import (
    DataQualitySummary,
    DataScope,
    EvidenceState,
    FieldEvidenceInput,
    ImportManifestInput,
    ImportReference,
    QualityStatus,
    ReadonlyDataService,
    ReportContractRegistry,
    ReportFieldPolicy,
    RowDisposition,
    RowIsolationIssue,
    SourceKind,
    content_digest,
    project_evidenced_value,
    sanitize_report_row,
    schema_fingerprint,
)


EXPORTED_AT = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)


def _policy() -> ReportFieldPolicy:
    return ReportFieldPolicy(
        report_type="orders",
        mapping_version="orders-v1",
        field_aliases={
            "订单编号": "order_id",
            "实付金额": "total_amount",
        },
        allowed_fields=frozenset({"order_id", "total_amount"}),
        required_fields=frozenset({"order_id"}),
        sensitive_fields=frozenset({"顾客姓名", "手机号", "收货地址"}),
    )


def _manifest(
    *,
    source_kind: SourceKind = SourceKind.ACTUAL,
    exported_at: datetime = EXPORTED_AT,
    content: bytes = b"order_id,total_amount\nORDER-1,88.00\n",
    parsed_rows: int = 1,
) -> ImportManifestInput:
    digest = content_digest(content)
    return ImportManifestInput(
        store_id="store-a",
        source_kind=source_kind,
        source_system="taobao_export",
        report_type="orders",
        report_period="2026-08-16",
        exported_at=exported_at,
        schema_fingerprint=schema_fingerprint(
            ["订单编号", "实付金额", "顾客姓名", "手机号", "收货地址"]
        ),
        content_digest=digest,
        mapping_version="orders-v1",
        parsed_rows=parsed_rows,
        data_as_of=exported_at,
        references=[
            ImportReference(
                kind="raw_file",
                reference=f"objects/readonly-imports/{digest}.csv",
                content_digest=digest,
            )
        ],
    )


def test_report_policy_is_authoritative_and_privacy_projection_drops_unsafe_columns() -> None:
    policy = _policy()
    registry = ReportContractRegistry()
    registry.register(policy)

    assert registry.get("orders", "orders-v1") is policy
    with pytest.raises(ValueError, match="duplicate_report_contract"):
        registry.register(policy)

    sanitized = sanitize_report_row(
        policy,
        {
            "订单编号": "ORDER-1",
            "实付金额": "88.00",
            "顾客姓名": "张三",
            "手机号": "13800138000",
            "收货地址": "上海市浦东新区测试路 1 号",
            "客服备注": "请拨打 13800138000 联系张三",
        },
    )

    assert sanitized.payload == {"order_id": "ORDER-1", "total_amount": "88.00"}
    assert sanitized.sensitive_fields_removed == 3
    assert sanitized.non_allowlisted_fields_removed == 1
    assert sanitized.log_projection() == {
        "report_type": "orders",
        "mapping_version": "orders-v1",
        "accepted_fields": ["order_id", "total_amount"],
        "accepted_field_count": 2,
        "sensitive_fields_removed": 3,
        "sensitive_values_removed": 0,
        "non_allowlisted_fields_removed": 1,
    }
    downstream = json.dumps(
        {
            "normalized": sanitized.payload,
            "model": sanitized.downstream_payload(),
            "evaluation": sanitized.downstream_payload(),
            "log": sanitized.log_projection(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    for secret in ("张三", "13800138000", "测试路", "客服备注"):
        assert secret not in downstream


def test_allowlisted_text_values_cannot_carry_customer_pii_downstream() -> None:
    policy = ReportFieldPolicy(
        report_type="orders",
        mapping_version="orders-v1",
        field_aliases={
            "订单编号": "order_id",
            "内部备注": "internal_note",
            "跟进说明": "follow_up_note",
            "配送说明": "delivery_note",
            "联系说明": "contact_note",
            "邮寄说明": "postal_note",
        },
        allowed_fields=frozenset(
            {
                "order_id",
                "internal_note",
                "follow_up_note",
                "delivery_note",
                "contact_note",
                "postal_note",
            }
        ),
        required_fields=frozenset({"order_id"}),
    )

    sanitized = sanitize_report_row(
        policy,
        {
            "订单编号": "ORDER-1",
            "内部备注": "顾客联系电话 138 0013 8000",
            "跟进说明": "联系邮箱 customer@example.com",
            "配送说明": "收货地址 上海市浦东新区测试路 1 号",
            "联系说明": "姓名 张三",
            "邮寄说明": "邮编 200120",
        },
    )

    assert sanitized.payload == {"order_id": "ORDER-1"}
    assert sanitized.sensitive_values_removed == 5
    projection = json.dumps(
        {
            "normalized": sanitized.payload,
            "model": sanitized.downstream_payload(),
            "log": sanitized.log_projection(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    for secret in (
        "138 0013 8000",
        "customer@example.com",
        "测试路",
        "张三",
        "200120",
    ):
        assert secret not in projection


def test_identity_email_and_postal_fields_are_intrinsically_sensitive() -> None:
    for field in ("identity_number", "email", "postal_code"):
        with pytest.raises(ValueError, match="sensitive_field_cannot_be_allowlisted"):
            ReportFieldPolicy(
                report_type="orders",
                mapping_version="orders-v1",
                allowed_fields=frozenset({field}),
            )

    for alias in ("身份证号", "电子邮箱", "邮政编码"):
        with pytest.raises(ValueError, match="sensitive_field_cannot_be_aliased"):
            ReportFieldPolicy(
                report_type="orders",
                mapping_version="orders-v1",
                field_aliases={alias: "order_id"},
                allowed_fields=frozenset({"order_id"}),
            )


def test_report_policy_rejects_ambiguous_normalized_field_names() -> None:
    with pytest.raises(ValueError, match="ambiguous_canonical_field"):
        ReportFieldPolicy(
            report_type="orders",
            mapping_version="orders-v1",
            allowed_fields=frozenset({"order_id", "orderid"}),
        )

    with pytest.raises(ValueError, match="ambiguous_report_field_alias"):
        ReportFieldPolicy(
            report_type="orders",
            mapping_version="orders-v1",
            field_aliases={"order-id": "total_amount"},
            allowed_fields=frozenset({"order_id", "total_amount"}),
        )


def test_report_policy_cannot_be_mutated_to_bypass_privacy_validation() -> None:
    policy = _policy()

    with pytest.raises(TypeError):
        policy.field_aliases["手机号"] = "order_id"

    sanitized = sanitize_report_row(policy, {"手机号": "13800138000", "订单编号": "ORDER-1"})
    with pytest.raises(TypeError):
        sanitized.payload["手机号"] = "13800138000"

    quality = DataQualitySummary(
        status=QualityStatus.PARTIAL,
        total_rows=2,
        accepted_rows=1,
        quarantined_rows=0,
        rejected_rows=1,
        issue_counts={"invalid_total_amount": 1},
    )
    with pytest.raises(TypeError):
        quality.issue_counts["phone_exposed"] = 1

    assert sanitized.payload == {"order_id": "ORDER-1"}
    assert quality.model_dump(mode="json")["issue_counts"] == {
        "invalid_total_amount": 1
    }


def test_sanitized_row_returns_an_independent_mutable_downstream_payload() -> None:
    sanitized = sanitize_report_row(_policy(), {"订单编号": "ORDER-1"})

    downstream = sanitized.downstream_payload()

    assert type(downstream) is dict
    assert downstream is not sanitized.payload
    downstream["order_id"] = "ORDER-2"
    assert sanitized.payload == {"order_id": "ORDER-1"}


def test_digest_reference_and_schema_fingerprint_define_controlled_file_boundary() -> None:
    raw = b"sku_id,on_hand\nSKU-1,3\n"
    digest = content_digest(raw)
    reference = ImportReference(
        kind="raw_file",
        reference=f"objects/readonly-imports/{digest}.csv",
        content_digest=digest.upper(),
    )

    assert reference.content_digest == digest
    assert schema_fingerprint(["sku_id", "on_hand"]) == schema_fingerprint(
        ["on_hand", "sku_id"]
    )
    assert schema_fingerprint(["sku_id", "on_hand"]) != schema_fingerprint(
        ["sku_id", "available"]
    )
    with pytest.raises(ValueError, match="storage_ref_credentials_forbidden"):
        ImportReference(
            kind="raw_file",
            reference=f"s3://user:secret@bucket/imports/{digest}.csv",
            content_digest=digest,
        )


def test_schema_fingerprint_rejects_normalized_duplicate_headers() -> None:
    collisions = (
        ("sku_id", "SKU_ID"),
        ("sku_id", " sku_id "),
        ("sku_id", "ＳＫＵ＿ＩＤ"),
    )

    for field_names in collisions:
        with pytest.raises(ValueError, match="duplicate_readonly_schema_field"):
            schema_fingerprint(field_names)


def test_manifest_replay_is_idempotent_and_source_version_conflicts_are_rejected(
    tmp_path,
) -> None:
    db = Database(tmp_path / "readonly-imports.sqlite3")
    db.initialize()
    service = ReadonlyDataService(db)
    issue = RowIsolationIssue(
        row_number=2,
        disposition=RowDisposition.REJECTED,
        reason="invalid_total_amount",
        field_keys=("total_amount",),
        raw_row_digest=content_digest(b"ORDER-2,not-a-number"),
    )
    manifest = _manifest(parsed_rows=2)

    first = service.record_import("tenant-a", manifest, row_issues=[issue])
    repeated = service.record_import("tenant-a", manifest, row_issues=[issue])

    assert first["write_status"] == "applied"
    assert repeated["write_status"] == "idempotent"
    assert repeated["import_id"] == first["import_id"]
    assert repeated["quality"] == {
        "status": "partial",
        "total_rows": 2,
        "accepted_rows": 1,
        "quarantined_rows": 0,
        "rejected_rows": 1,
        "issue_counts": {"invalid_total_amount": 1},
    }
    assert service.list_row_issues("tenant-a", import_id=first["import_id"]) == [
        {
            "issue_id": service.list_row_issues(
                "tenant-a", import_id=first["import_id"]
            )[0]["issue_id"],
            "import_id": first["import_id"],
            "tenant_id": "tenant-a",
            "store_id": "store-a",
            "row_number": 2,
            "disposition": "rejected",
            "reason": "invalid_total_amount",
            "field_keys": ["total_amount"],
            "raw_row_digest": issue.raw_row_digest,
            "created_at": service.list_row_issues(
                "tenant-a", import_id=first["import_id"]
            )[0]["created_at"],
        }
    ]

    conflicting = _manifest(content=b"order_id,total_amount\nORDER-1,99.00\n")
    with pytest.raises(SourceVersionError, match="source_version_conflict"):
        service.record_import("tenant-a", conflicting)
    stale = _manifest(exported_at=EXPORTED_AT - timedelta(minutes=1))
    with pytest.raises(SourceVersionError, match="stale_source_version"):
        service.record_import("tenant-a", stale)


def test_manifest_quality_counts_are_derived_from_parser_observation_and_issues(
    tmp_path,
) -> None:
    manifest = _manifest(parsed_rows=2)
    issue = RowIsolationIssue(
        row_number=2,
        disposition=RowDisposition.REJECTED,
        reason="invalid_total_amount",
        field_keys=("total_amount",),
        raw_row_digest=content_digest(b"ORDER-2,not-a-number"),
    )
    db = Database(tmp_path / "readonly-derived-quality.sqlite3")
    db.initialize()

    imported = ReadonlyDataService(db).record_import(
        "tenant-a", manifest, row_issues=[issue]
    )

    assert imported["accepted_rows"] == 1
    assert imported["quarantined_rows"] == 0
    assert imported["rejected_rows"] == 1
    assert imported["quality"]["total_rows"] == 2
    with pytest.raises(ValueError, match="extra_forbidden"):
        ImportManifestInput(**manifest.model_dump(), accepted_rows=99)


def test_missing_evidence_has_no_manifest_or_numeric_zero_and_demo_is_not_operational(
    tmp_path,
) -> None:
    db = Database(tmp_path / "readonly-evidence.sqlite3")
    db.initialize()
    service = ReadonlyDataService(db)
    actual = service.record_import("tenant-a", _manifest())
    demo = service.record_import(
        "tenant-a",
        _manifest(
            source_kind=SourceKind.DEMO,
            content=b"order_id,total_amount\nDEMO-1,18.00\n",
        ),
    )
    missing = service.record_field_evidence(
        "tenant-a",
        FieldEvidenceInput(
            store_id="store-a",
            field_key="refund_amount",
            scope="store",
            evidence_state=EvidenceState.MISSING,
            reason="refund_report_not_imported",
        ),
    )
    service.record_field_evidence(
        "tenant-a",
        FieldEvidenceInput(
            store_id="store-a",
            field_key="order_total",
            scope="store",
            evidence_state=EvidenceState.ACTUAL,
            reason="orders_imported",
            data_as_of=EXPORTED_AT,
            import_id=actual["import_id"],
        ),
    )
    service.record_field_evidence(
        "tenant-a",
        FieldEvidenceInput(
            store_id="store-a",
            field_key="demo_order_total",
            scope="store",
            evidence_state=EvidenceState.DEMO,
            reason="demo_orders_imported",
            data_as_of=EXPORTED_AT,
            import_id=demo["import_id"],
        ),
    )

    assert missing["import_id"] is None
    assert project_evidenced_value(EvidenceState.MISSING, None) is None
    with pytest.raises(ValueError, match="missing_evidence_must_not_have_value"):
        project_evidenced_value(EvidenceState.MISSING, 0)
    assert {row["import_id"] for row in service.list_imports("tenant-a")} == {
        actual["import_id"]
    }
    assert {
        row["import_id"]
        for row in service.list_imports("tenant-a", scope=DataScope.ALL)
    } == {actual["import_id"], demo["import_id"]}
    assert {
        row["field_key"] for row in service.list_field_evidence("tenant-a")
    } == {"refund_amount", "order_total"}
    assert {
        row["field_key"]
        for row in service.list_field_evidence("tenant-a", scope=DataScope.ALL)
    } == {"refund_amount", "order_total", "demo_order_total"}


def test_field_evidence_latest_follows_append_order_and_only_current_replay_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    db = Database(tmp_path / "readonly-evidence-order.sqlite3")
    db.initialize()
    service = ReadonlyDataService(db)
    monkeypatch.setattr(
        readonly_service_module,
        "utc_now",
        lambda: "2026-08-17T00:00:00+00:00",
    )
    evidence_ids = iter(("f" * 32, "0" * 32, "1" * 32))
    monkeypatch.setattr(
        readonly_service_module.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex=next(evidence_ids)),
    )

    def record(reason: str) -> dict[str, object]:
        return service.record_field_evidence(
            "tenant-a",
            FieldEvidenceInput(
                store_id="store-a",
                field_key="refund_amount",
                scope="store",
                evidence_state=EvidenceState.MISSING,
                reason=reason,
            ),
        )

    first = record("source_unavailable")
    second = record("awaiting_export")

    assert service.list_field_evidence("tenant-a")[0]["evidence_id"] == second[
        "evidence_id"
    ]
    repeated = record("awaiting_export")
    assert repeated["write_status"] == "idempotent"
    assert repeated["evidence_id"] == second["evidence_id"]

    returned = record("source_unavailable")
    assert returned["write_status"] == "applied"
    assert returned["evidence_id"] != first["evidence_id"]
    assert service.list_field_evidence("tenant-a")[0]["evidence_id"] == returned[
        "evidence_id"
    ]


def test_field_evidence_cannot_claim_data_newer_than_its_manifest(tmp_path) -> None:
    db = Database(tmp_path / "readonly-evidence-freshness.sqlite3")
    db.initialize()
    service = ReadonlyDataService(db)
    imported = service.record_import("tenant-a", _manifest())

    with pytest.raises(
        ValueError, match="field_evidence_data_as_of_after_manifest"
    ):
        service.record_field_evidence(
            "tenant-a",
            FieldEvidenceInput(
                store_id="store-a",
                field_key="order_total",
                scope="store",
                evidence_state=EvidenceState.ACTUAL,
                reason="orders_imported",
                data_as_of=EXPORTED_AT + timedelta(seconds=1),
                import_id=imported["import_id"],
            ),
        )


def test_import_and_evidence_reads_are_tenant_and_store_scoped(tmp_path) -> None:
    db = Database(tmp_path / "readonly-scope.sqlite3")
    db.initialize()
    service = ReadonlyDataService(db)
    imported = service.record_import("tenant-a", _manifest())

    assert service.list_imports("tenant-b") == []
    assert service.list_imports("tenant-a", store_id="store-b") == []
    with pytest.raises(ValueError, match="readonly_import_not_found"):
        service.get_import("tenant-b", imported["import_id"])
    with pytest.raises(ValueError, match="readonly_import_not_found"):
        service.record_field_evidence(
            "tenant-b",
            FieldEvidenceInput(
                store_id="store-a",
                field_key="order_total",
                scope="store",
                evidence_state=EvidenceState.ACTUAL,
                reason="orders_imported",
                data_as_of=EXPORTED_AT,
                import_id=imported["import_id"],
            ),
        )


def test_v34_adds_readonly_import_contract_tables_once(tmp_path) -> None:
    db = Database(tmp_path / "readonly-v34.sqlite3")
    db.initialize()
    db.initialize()

    with db.connect() as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        migrations = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        manifest_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(readonly_import_manifests)")
        }

    assert 34 in migrations
    assert {
        "readonly_import_manifests",
        "readonly_field_evidence",
        "readonly_import_row_issues",
    } <= tables
    assert {
        "import_id",
        "tenant_id",
        "store_id",
        "source_kind",
        "source_system",
        "report_type",
        "report_period",
        "exported_at",
        "imported_at",
        "schema_fingerprint",
        "content_digest",
        "mapping_version",
        "accepted_rows",
        "quarantined_rows",
        "rejected_rows",
        "data_as_of",
        "references_json",
        "quality_json",
        "payload_hash",
    } <= manifest_columns


def test_v33_database_upgrades_to_v34_without_losing_existing_data(tmp_path) -> None:
    db = Database(tmp_path / "readonly-v33-upgrade.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in [*range(1, 31), 32, 33]:
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-08-17T00:00:00+00:00"),
            )
        conn.execute(
            """
            INSERT INTO creative_assets(
                asset_id, tenant_id, sha256, mime_type, width, height, storage_ref,
                source_ref, feature_schema_version, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset-v33",
                "tenant-a",
                "a" * 64,
                "image/png",
                1,
                1,
                "objects/assets/a.png",
                None,
                "traffic-creative-features-v1",
                "b" * 64,
                "2026-08-17T00:00:00+00:00",
                "2026-08-17T00:00:00+00:00",
            ),
        )
        conn.execute("PRAGMA user_version = 33")

    db.initialize()

    with db.connect() as conn:
        migrations = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        preserved = conn.execute(
            "SELECT asset_id FROM creative_assets WHERE asset_id='asset-v33'"
        ).fetchone()

    assert 34 in migrations
    assert {
        "readonly_import_manifests",
        "readonly_field_evidence",
        "readonly_import_row_issues",
    } <= tables
    assert preserved["asset_id"] == "asset-v33"


def test_v34_sql_enum_checks_match_readonly_contract_enums(tmp_path) -> None:
    db = Database(tmp_path / "readonly-v34-enums.sqlite3")
    db.initialize()
    checks = (
        ("readonly_import_manifests", "source_kind", SourceKind),
        ("readonly_field_evidence", "evidence_state", EvidenceState),
        ("readonly_import_row_issues", "disposition", RowDisposition),
    )

    with db.connect() as conn:
        table_sql = {
            table: conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
            for table, _column, _enum_type in checks
        }

    for table, column, enum_type in checks:
        match = re.search(
            rf"CHECK\s*\(\s*{re.escape(column)}\s+IN\s*\(([^)]*)\)\s*\)",
            table_sql[table],
            flags=re.IGNORECASE,
        )
        assert match is not None, f"missing enum CHECK for {table}.{column}"
        sql_values = set(re.findall(r"'([^']+)'", match.group(1)))
        assert sql_values == {member.value for member in enum_type}


def test_v34_database_enforces_evidence_scope_source_and_missing_boundary(tmp_path) -> None:
    db = Database(tmp_path / "readonly-v34-boundary.sqlite3")
    db.initialize()
    imported = ReadonlyDataService(db).record_import("tenant-a", _manifest())
    evidence_values = (
        "field-evidence-invalid",
        "tenant-a",
        "store-b",
        "order_total",
        "store",
        "actual",
        "orders_imported",
        EXPORTED_AT.isoformat(),
        None,
        imported["import_id"],
        "0" * 64,
        EXPORTED_AT.isoformat(),
    )

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO readonly_field_evidence(
                    evidence_id, tenant_id, store_id, field_key, scope,
                    evidence_state, reason, data_as_of, source_reference,
                    import_id, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                evidence_values,
            )

    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO readonly_field_evidence(
                    evidence_id, tenant_id, store_id, field_key, scope,
                    evidence_state, reason, data_as_of, source_reference,
                    import_id, payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, 'missing', ?, ?, NULL, ?, ?, ?)
                """,
                (
                    "field-evidence-invalid-missing",
                    "tenant-a",
                    "store-a",
                    "refund_amount",
                    "store",
                    "refund_report_not_imported",
                    EXPORTED_AT.isoformat(),
                    imported["import_id"],
                    "1" * 64,
                    EXPORTED_AT.isoformat(),
                ),
            )


def test_v34_readonly_contract_records_are_immutable(tmp_path) -> None:
    db = Database(tmp_path / "readonly-v34-immutable.sqlite3")
    db.initialize()
    service = ReadonlyDataService(db)
    imported = service.record_import("tenant-a", _manifest())
    evidence = service.record_field_evidence(
        "tenant-a",
        FieldEvidenceInput(
            store_id="store-a",
            field_key="order_total",
            scope="store",
            evidence_state=EvidenceState.ACTUAL,
            reason="orders_imported",
            import_id=imported["import_id"],
        ),
    )

    with pytest.raises(sqlite3.IntegrityError, match="readonly_import_manifest_immutable"):
        with db.connect() as conn:
            conn.execute(
                "UPDATE readonly_import_manifests SET report_period=? WHERE import_id=?",
                ("changed", imported["import_id"]),
            )
    with pytest.raises(sqlite3.IntegrityError, match="readonly_field_evidence_immutable"):
        with db.connect() as conn:
            conn.execute(
                "DELETE FROM readonly_field_evidence WHERE evidence_id=?",
                (evidence["evidence_id"],),
            )
