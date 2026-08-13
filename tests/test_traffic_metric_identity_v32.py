from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ecommerce_agent.business.source_versioning import payload_digest
from ecommerce_agent.database import Database
from ecommerce_agent.traffic_lab import (
    CreativeAssetCreate,
    ListingRevisionCreate,
    TrafficLabService,
    TrafficMetricBucketUpsert,
)


BASE_TIME = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
LEGACY_UNSCOPED = "legacy_unscoped"


def _revision(
    service: TrafficLabService,
    tenant_id: str,
    connector_id: str,
) -> dict[str, object]:
    asset = service.register_asset(
        tenant_id,
        CreativeAssetCreate(
            sha256="a" * 64,
            mime_type="image/png",
            width=1200,
            height=1200,
            storage_ref="objects/traffic-lab/metric-identity.png",
            source_ref="fixture://traffic-metric-identity",
            feature_schema_version="image-v1",
        ),
    )
    return service.create_revision(
        tenant_id,
        ListingRevisionCreate(
            connector_id=connector_id,
            store_id="store-a",
            item_id="item-a",
            sku_id="sku-a",
            revision_no=1,
            title=f"{connector_id} revision",
            main_image_asset_id=str(asset["asset_id"]),
            sale_price="109.00",
            attributes={"stock_status": "in_stock"},
            active_from=BASE_TIME - timedelta(days=1),
            active_to=BASE_TIME + timedelta(days=3),
            source_updated_at=BASE_TIME - timedelta(days=1),
        ),
    )


def _metric(
    listing_revision_id: str | None,
    *,
    connector_id: str | None,
    source_id: str = "native-metric-1",
    data_as_of: datetime = BASE_TIME + timedelta(hours=2),
    clicks: int = 50,
) -> TrafficMetricBucketUpsert:
    return TrafficMetricBucketUpsert(
        listing_revision_id=listing_revision_id,
        connector_id=connector_id,
        metric_start=BASE_TIME,
        metric_end=BASE_TIME + timedelta(hours=1),
        bucket_granularity="hour",
        traffic_source="recommend",
        impressions=1000,
        clicks=clicks,
        visitors=clicks,
        favorites=5,
        cart_adds=3,
        orders=2,
        sales_amount="218.00",
        ad_spend="0",
        search_impressions=300,
        recommend_impressions=700,
        data_as_of=data_as_of,
        source_id=source_id,
    )


def _unique_index_shapes(conn, table: str) -> set[tuple[str, ...]]:
    shapes: set[tuple[str, ...]] = set()
    for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if not bool(index[2]):
            continue
        shapes.add(
            tuple(
                str(column[2])
                for column in conn.execute(
                    f"PRAGMA index_info({index[1]})"
                ).fetchall()
            )
        )
    return shapes


def test_same_native_source_id_is_independent_across_connectors_and_tenants(
    tmp_path,
) -> None:
    db = Database(tmp_path / "triple-identity.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    revision_a = _revision(service, "tenant-a", "connector-a")
    revision_b = _revision(service, "tenant-a", "connector-b")
    revision_other_tenant = _revision(service, "tenant-b", "connector-a")

    first = service.upsert_metric_bucket(
        "tenant-a",
        _metric(str(revision_a["id"]), connector_id=None),
    )
    second = service.upsert_metric_bucket(
        "tenant-a",
        _metric(str(revision_b["id"]), connector_id="connector-b"),
    )
    other_tenant = service.upsert_metric_bucket(
        "tenant-b",
        _metric(str(revision_other_tenant["id"]), connector_id="connector-a"),
    )

    assert first["connector_id"] == "connector-a"
    assert second["connector_id"] == "connector-b"
    assert other_tenant["connector_id"] == "connector-a"
    assert first["id"] != second["id"]
    assert {
        (item["connector_id"], item["source_id"])
        for item in service.list_metric_buckets("tenant-a")
    } == {
        ("connector-a", "native-metric-1"),
        ("connector-b", "native-metric-1"),
    }
    assert service.upsert_metric_bucket(
        "tenant-a",
        _metric(str(revision_a["id"]), connector_id=None),
    )["write_status"] == "idempotent"
    assert service.upsert_metric_bucket(
        "tenant-a",
        _metric(str(revision_b["id"]), connector_id="connector-b"),
    )["write_status"] == "idempotent"


def test_same_connector_retains_source_version_semantics(tmp_path) -> None:
    db = Database(tmp_path / "source-version.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    revision = _revision(service, "tenant-a", "connector-a")
    original = _metric(str(revision["id"]), connector_id="connector-a")

    created = service.upsert_metric_bucket("tenant-a", original)
    repeated = service.upsert_metric_bucket("tenant-a", original)
    with pytest.raises(ValueError, match="stale_source_version"):
        service.upsert_metric_bucket(
            "tenant-a",
            original.model_copy(
                update={"data_as_of": BASE_TIME + timedelta(hours=1)}
            ),
        )
    with pytest.raises(ValueError, match="source_version_conflict"):
        service.upsert_metric_bucket(
            "tenant-a",
            original.model_copy(update={"clicks": 51, "visitors": 51}),
        )
    updated = service.upsert_metric_bucket(
        "tenant-a",
        original.model_copy(
            update={
                "clicks": 52,
                "visitors": 52,
                "data_as_of": BASE_TIME + timedelta(hours=3),
            }
        ),
    )

    assert created["version"] == repeated["version"] == 1
    assert repeated["write_status"] == "idempotent"
    assert updated["id"] == created["id"]
    assert updated["version"] == 2


def test_normal_and_quarantine_are_mutually_exclusive_only_within_triple_identity(
    tmp_path,
) -> None:
    db = Database(tmp_path / "triple-state.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    revision_a = _revision(service, "tenant-a", "connector-a")
    revision_b = _revision(service, "tenant-a", "connector-b")
    source_id = "shared-native-source"

    accepted_a = service.ingest_metric_bucket(
        "tenant-a",
        _metric(
            str(revision_a["id"]),
            connector_id="connector-a",
            source_id=source_id,
        ),
    )
    quarantined_b = service.ingest_metric_bucket(
        "tenant-a",
        _metric(
            "missing-revision",
            connector_id="connector-b",
            source_id=source_id,
            data_as_of=BASE_TIME + timedelta(hours=3),
        ),
    )

    assert accepted_a["disposition"] == "accepted"
    assert quarantined_b["disposition"] == "quarantined"
    assert {
        item["connector_id"] for item in service.list_metric_buckets("tenant-a")
    } == {"connector-a"}
    assert {
        item["connector_id"] for item in service.list_metric_quarantine("tenant-a")
    } == {"connector-b"}

    accepted_b = service.ingest_metric_bucket(
        "tenant-a",
        _metric(
            str(revision_b["id"]),
            connector_id="connector-b",
            source_id=source_id,
            data_as_of=BASE_TIME + timedelta(hours=4),
        ),
    )
    quarantined_a = service.ingest_metric_bucket(
        "tenant-a",
        _metric(
            "missing-revision",
            connector_id="connector-a",
            source_id=source_id,
            data_as_of=BASE_TIME + timedelta(hours=5),
        ),
    )

    assert accepted_b["disposition"] == "accepted"
    assert quarantined_a["connector_id"] == "connector-a"
    assert {
        item["connector_id"] for item in service.list_metric_buckets("tenant-a")
    } == {"connector-b"}
    assert {
        item["connector_id"] for item in service.list_metric_quarantine("tenant-a")
    } == {"connector-a"}


def test_revision_connector_mismatch_and_unscoped_new_quarantine_are_rejected(
    tmp_path,
) -> None:
    db = Database(tmp_path / "identity-validation.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    revision = _revision(service, "tenant-a", "connector-a")

    with pytest.raises(ValueError, match="listing_revision_identity_mismatch"):
        service.upsert_metric_bucket(
            "tenant-a",
            _metric(str(revision["id"]), connector_id="connector-b"),
        )
    with pytest.raises(ValueError, match="listing_revision_not_found"):
        service.upsert_metric_bucket(
            "tenant-b",
            _metric(str(revision["id"]), connector_id="connector-a"),
        )
    with pytest.raises(ValueError, match="metric_connector_required"):
        service.quarantine_metric_bucket(
            "tenant-a",
            _metric("missing-revision", connector_id=None),
            reason_code="listing_revision_not_found",
        )


def test_revision_only_out_of_window_metric_uses_derived_connector_for_quarantine(
    tmp_path,
) -> None:
    db = Database(tmp_path / "revision-only-quarantine.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    revision = _revision(service, "tenant-a", "connector-a")
    outside = _metric(
        str(revision["id"]),
        connector_id=None,
        source_id="revision-only-outside",
        data_as_of=BASE_TIME + timedelta(days=3, hours=2),
    ).model_copy(
        update={
            "metric_start": BASE_TIME + timedelta(days=3),
            "metric_end": BASE_TIME + timedelta(days=3, hours=1),
        }
    )

    quarantined = service.ingest_metric_bucket("tenant-a", outside)

    assert quarantined["disposition"] == "quarantined"
    assert quarantined["reason_code"] == "metric_outside_revision_window"
    assert quarantined["connector_id"] == "connector-a"
    assert {
        field: quarantined["payload"][field]
        for field in ("connector_id", "store_id", "item_id", "sku_id")
    } == {
        "connector_id": "connector-a",
        "store_id": "store-a",
        "item_id": "item-a",
        "sku_id": "sku-a",
    }
    assert service.list_metric_buckets("tenant-a") == []


def test_resolved_connector_is_canonical_for_replay_and_same_time_promotion(
    tmp_path,
) -> None:
    db = Database(tmp_path / "canonical-connector-hash.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    revision = _revision(service, "tenant-a", "connector-a")

    explicit_quarantine = _metric(
        str(revision["id"]),
        connector_id="connector-a",
        source_id="same-time-promotion",
    )
    quarantined = service.quarantine_metric_bucket(
        "tenant-a",
        explicit_quarantine,
        reason_code="metric_outside_revision_window",
    )
    promoted = service.ingest_metric_bucket(
        "tenant-a",
        explicit_quarantine.model_copy(update={"connector_id": None}),
    )

    assert promoted["disposition"] == "accepted"
    assert promoted["payload_hash"] == quarantined["payload_hash"]
    assert promoted["version"] == quarantined["version"]
    assert service.list_metric_quarantine("tenant-a") == []

    revision_only = _metric(
        str(revision["id"]),
        connector_id=None,
        source_id="omit-explicit-replay",
    )
    created = service.upsert_metric_bucket("tenant-a", revision_only)
    repeated = service.upsert_metric_bucket(
        "tenant-a",
        revision_only.model_copy(
            update={
                "connector_id": "connector-a",
                "store_id": "store-a",
                "item_id": "item-a",
                "sku_id": "sku-a",
            }
        ),
    )

    assert repeated["write_status"] == "idempotent"
    assert repeated["id"] == created["id"]
    assert repeated["version"] == created["version"]
    assert repeated["payload_hash"] == created["payload_hash"]


def _seed_v30_database(path, *, conflicting_identity: bool = False) -> Database:
    db = Database(path)
    applied_at = "2026-08-01T00:00:00+00:00"
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 31):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, applied_at),
            )
        conn.execute(
            """
            INSERT INTO creative_assets(
                asset_id, tenant_id, sha256, mime_type, width, height,
                storage_ref, source_ref, feature_schema_version, payload_hash,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset-v30", "tenant-a", "a" * 64, "image/png", 1200, 1200,
                "objects/traffic-lab/v30.png", "fixture://v30", "image-v1",
                "asset-payload-v30", applied_at, applied_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO listing_revisions(
                id, tenant_id, connector_id, store_id, item_id, sku_id,
                revision_no, title, main_image_asset_id, sale_price,
                attributes_json, active_from, active_to, source_updated_at,
                payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "revision-v30", "tenant-a", "connector-a", "store-a", "item-a",
                "sku-a", 1, "v30 revision", "asset-v30", "109.00", "{}",
                "2026-07-31T00:00:00+00:00", "2026-08-03T00:00:00+00:00",
                applied_at, "revision-payload-v30", applied_at, applied_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO traffic_metric_buckets(
                id, tenant_id, listing_revision_id, metric_start, metric_end,
                bucket_granularity, traffic_source, impressions, clicks, visitors,
                favorites, cart_adds, orders, sales_amount, ad_spend,
                search_impressions, recommend_impressions, data_as_of, source_id,
                payload_hash, quality_flags_json, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "metric-v30", "tenant-a", "revision-v30",
                "2026-08-01T00:00:00+00:00", "2026-08-01T01:00:00+00:00",
                "hour", "recommend", 1000, 50, 50, 5, 3, 2, "218.00", "0",
                300, 700, "2026-08-01T02:00:00+00:00", "shared-native",
                "metric-payload-v30", "[]", 3, applied_at, applied_at,
            ),
        )
        explicit_connector = "connector-a" if conflicting_identity else "connector-b"
        explicit_payload = json.dumps(
            {
                "connector_id": explicit_connector,
                "source_id": "shared-native",
                "listing_revision_id": None,
            },
            sort_keys=True,
        )
        conn.execute(
            """
            INSERT INTO traffic_metric_quarantine(
                quarantine_id, tenant_id, source_id, reason_code, payload_json,
                data_as_of, payload_hash, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "quarantine-v30", "tenant-a", "shared-native",
                "listing_revision_missing", explicit_payload,
                "2026-08-01T02:00:00+00:00", "quarantine-payload-v30", 4,
                applied_at, applied_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO traffic_metric_quarantine(
                quarantine_id, tenant_id, source_id, reason_code, payload_json,
                data_as_of, payload_hash, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "quarantine-legacy-v30", "tenant-a", "legacy-native",
                "listing_revision_missing",
                json.dumps({"source_id": "legacy-native", "listing_revision_id": None}),
                "2026-08-01T03:00:00+00:00", "legacy-payload-v30", 2,
                applied_at, applied_at,
            ),
        )
    return db


def test_v30_to_v32_migrates_triple_identity_and_legacy_quarantine_without_guessing(
    tmp_path,
) -> None:
    db = _seed_v30_database(tmp_path / "v30-to-v32.sqlite3")

    db.initialize()
    db.initialize()

    with db.connect() as conn:
        migrations = {
            int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
        }
        accepted = dict(
            conn.execute(
                "SELECT * FROM traffic_metric_buckets WHERE id='metric-v30'"
            ).fetchone()
        )
        quarantined = {
            str(row["quarantine_id"]): dict(row)
            for row in conn.execute(
                "SELECT * FROM traffic_metric_quarantine ORDER BY quarantine_id"
            ).fetchall()
        }
        bucket_indexes = _unique_index_shapes(conn, "traffic_metric_buckets")
        quarantine_indexes = _unique_index_shapes(conn, "traffic_metric_quarantine")
        competitive_match_indexes = _unique_index_shapes(
            conn, "competitive_entity_matches"
        )
        competitive_signal_indexes = _unique_index_shapes(conn, "competitive_signals")

    assert 32 in migrations
    assert accepted["connector_id"] == "connector-a"
    assert accepted["source_id"] == "shared-native"
    assert accepted["payload_hash"] == "metric-payload-v30"
    assert accepted["version"] == 3
    assert quarantined["quarantine-v30"]["connector_id"] == "connector-b"
    assert quarantined["quarantine-v30"]["payload_hash"] == "quarantine-payload-v30"
    assert quarantined["quarantine-v30"]["version"] == 4
    assert quarantined["quarantine-legacy-v30"]["connector_id"] == LEGACY_UNSCOPED
    assert quarantined["quarantine-legacy-v30"]["payload_hash"] == "legacy-payload-v30"
    assert quarantined["quarantine-legacy-v30"]["version"] == 2
    assert ("tenant_id", "connector_id", "source_id") in bucket_indexes
    assert ("tenant_id", "connector_id", "source_id") in quarantine_indexes
    assert ("tenant_id", "source_id") not in bucket_indexes
    assert ("tenant_id", "source_id") not in quarantine_indexes
    assert ("tenant_id", "connector_id", "source_id") in competitive_match_indexes
    assert ("tenant_id", "connector_id", "source_id") in competitive_signal_indexes
    visible_metrics = TrafficLabService(db).list_metric_buckets("tenant-a")
    assert [item["id"] for item in visible_metrics] == ["metric-v30"]
    assert all(item["connector_id"] != LEGACY_UNSCOPED for item in visible_metrics)


def test_v30_revision_only_hash_remains_idempotent_after_v32_identity_resolution(
    tmp_path,
) -> None:
    db = _seed_v30_database(tmp_path / "v30-replay.sqlite3")
    value = _metric(
        "revision-v30",
        connector_id=None,
        source_id="shared-native",
    )
    legacy_payload = value.model_dump(mode="json")
    for field in ("connector_id", "store_id", "item_id", "sku_id"):
        legacy_payload.pop(field)
    legacy_payload.update(
        {
            "metric_start": value.metric_start.astimezone(UTC).isoformat(),
            "metric_end": value.metric_end.astimezone(UTC).isoformat(),
            "data_as_of": value.data_as_of.astimezone(UTC).isoformat(),
        }
    )
    legacy_hash = payload_digest(legacy_payload)
    with db.connect() as conn:
        conn.execute(
            "UPDATE traffic_metric_buckets SET payload_hash=? WHERE id='metric-v30'",
            (legacy_hash,),
        )

    db.initialize()
    repeated = TrafficLabService(db).upsert_metric_bucket("tenant-a", value)

    assert repeated["id"] == "metric-v30"
    assert repeated["write_status"] == "idempotent"
    assert repeated["payload_hash"] == legacy_hash
    assert repeated["version"] == 3


def test_v32_migration_rejects_existing_normal_quarantine_triple_conflict(
    tmp_path,
) -> None:
    db = _seed_v30_database(
        tmp_path / "v30-conflict.sqlite3",
        conflicting_identity=True,
    )

    with pytest.raises(ValueError, match="metric_identity_state_conflict"):
        db.initialize()

    with db.connect() as conn:
        migrations = {
            int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations")
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM traffic_metric_buckets"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM traffic_metric_quarantine"
        ).fetchone()[0] == 2
    assert 32 not in migrations
