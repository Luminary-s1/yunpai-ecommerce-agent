from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime

from ecommerce_agent.business.source_versioning import payload_digest
from ecommerce_agent.connectors import PullRequest, VirtualTaobaoConnector
from ecommerce_agent.database import Database
from ecommerce_agent.traffic_lab import (
    CreativeAssetCreate,
    ListingRevisionCreate,
    TrafficLabIngestionService,
    TrafficLabService,
    TrafficMetricBucketUpsert,
)


def _seed_revision(
    db: Database,
    *,
    tenant_id: str = "tenant-a",
    active_from: datetime = datetime(2026, 7, 31, 0, 0, tzinfo=UTC),
    active_to: datetime = datetime(2026, 8, 4, 0, 0, tzinfo=UTC),
) -> TrafficLabService:
    service = TrafficLabService(db)
    asset = service.register_asset(
        tenant_id,
        CreativeAssetCreate(
            sha256="c" * 64,
            mime_type="image/png",
            width=1200,
            height=1200,
            storage_ref="objects/traffic-lab/import-fixture.png",
            source_ref="fixture://traffic-import",
            feature_schema_version="image-v1",
        ),
    )
    service.create_revision(
        tenant_id,
        ListingRevisionCreate(
            connector_id="virtual_taobao",
            store_id="store-001",
            item_id="item-001",
            sku_id="sku-001",
            revision_no=1,
            title="流量导入测试商品",
            main_image_asset_id=asset["asset_id"],
            sale_price="109.00",
            attributes={"stock_status": "in_stock"},
            active_from=active_from,
            active_to=active_to,
            source_updated_at=active_from,
        ),
    )
    return service


def test_csv_import_normalizes_hour_and_day_and_replay_is_idempotent(tmp_path) -> None:
    db = Database(tmp_path / "traffic-import.sqlite3")
    db.initialize()
    domain = _seed_revision(db)
    ingestion = TrafficLabIngestionService(db)
    content = (
        "store_id,item_id,sku_id,metric_start,bucket_granularity,"
        "traffic_source,impressions,clicks,visitors,orders,sales_amount,data_as_of\n"
        "store-001,item-001,sku-001,2026-08-01 08:00,小时,"
        "recommend,1000,80,75,2,218.00,2026-08-01 10:00\n"
        "store-001,item-001,sku-001,2026-08-02,日级,"
        "recommend,12000,840,800,20,2180.00,2026-08-03\n"
    )

    first = ingestion.import_metrics(
        "tenant-a",
        connector_id="virtual_taobao",
        source_format="csv",
        content=content,
        source_timezone="Asia/Shanghai",
    )
    assert first["total_rows"] == 2
    assert first["accepted_rows"] == 2
    assert first["quarantined_rows"] == 0
    assert first["rejected_rows"] == 0
    assert first["granularity_counts"] == {"hour": 1, "day": 1}
    assert first["applied"] == 2

    hourly = domain.list_metric_buckets(
        "tenant-a", bucket_granularity="hour", limit=100
    )
    daily = domain.list_metric_buckets(
        "tenant-a", bucket_granularity="day", limit=100
    )
    assert len(hourly) == len(daily) == 1
    assert hourly[0]["metric_start"] == "2026-08-01T00:00:00+00:00"
    assert hourly[0]["metric_end"] == "2026-08-01T01:00:00+00:00"
    assert daily[0]["metric_start"] == "2026-08-01T16:00:00+00:00"
    assert daily[0]["metric_end"] == "2026-08-02T16:00:00+00:00"
    assert hourly[0]["source_id"].startswith("traffic-import-")
    assert daily[0]["source_id"].startswith("traffic-import-")

    replay = ingestion.import_metrics(
        "tenant-a",
        connector_id="virtual_taobao",
        source_format="csv",
        content=content,
        source_timezone="Asia/Shanghai",
    )
    assert replay["applied"] == 0
    assert replay["idempotent"] == 2
    assert len(domain.list_metric_buckets("tenant-a", limit=100)) == 2


def test_json_import_isolates_unattributed_rows_without_failing_the_batch(tmp_path) -> None:
    db = Database(tmp_path / "traffic-json-import.sqlite3")
    db.initialize()
    domain = _seed_revision(
        db,
        active_from=datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
        active_to=datetime(2026, 8, 2, 0, 0, tzinfo=UTC),
    )
    ingestion = TrafficLabIngestionService(db)
    common = {
        "store_id": "store-001",
        "item_id": "item-001",
        "sku_id": "sku-001",
        "bucket_granularity": "hour",
        "traffic_source": "recommend",
        "visitors": 70,
        "orders": 2,
        "sales_amount": "218.00",
    }
    content = json.dumps(
        {
            "records": [
                {
                    **common,
                    "source_id": "json-valid-001",
                    "metric_start": "2026-08-01T01:00:00Z",
                    "impressions": 1000,
                    "clicks": 80,
                    "data_as_of": "2026-08-01T03:00:00Z",
                },
                {
                    **common,
                    "source_id": "json-outside-001",
                    "metric_start": "2026-08-03T01:00:00Z",
                    "impressions": 900,
                    "clicks": 70,
                    "data_as_of": "2026-08-03T03:00:00Z",
                },
                {
                    **common,
                    "source_id": "json-invalid-001",
                    "metric_start": "2026-08-01T02:00:00Z",
                    "impressions": 10,
                    "clicks": 11,
                    "data_as_of": "2026-08-01T03:00:00Z",
                },
            ]
        }
    )

    result = ingestion.import_metrics(
        "tenant-a",
        connector_id="virtual_taobao",
        source_format="json",
        content=content,
    )
    assert result["total_rows"] == 3
    assert result["accepted_rows"] == 1
    assert result["quarantined_rows"] == 1
    assert result["rejected_rows"] == 1
    assert result["quarantined"][0]["reason_code"] == "metric_outside_revision_window"
    assert "clicks_cannot_exceed_impressions" in result["rejected"][0]["reason"]
    assert len(domain.list_metric_buckets("tenant-a", limit=100)) == 1
    assert len(domain.list_metric_quarantine("tenant-a", limit=100)) == 1


def test_json_import_quarantines_bucket_matching_overlapping_revisions(tmp_path) -> None:
    db = Database(tmp_path / "traffic-ambiguous-revision.sqlite3")
    db.initialize()
    domain = _seed_revision(db)
    existing = domain.list_revisions("tenant-a", limit=1)[0]
    domain.create_revision(
        "tenant-a",
        ListingRevisionCreate(
            connector_id="virtual_taobao",
            store_id="store-001",
            item_id="item-001",
            sku_id="sku-001",
            revision_no=2,
            title="流量导入测试商品重叠版本",
            main_image_asset_id=existing["main_image_asset_id"],
            sale_price="109.00",
            attributes={"stock_status": "in_stock"},
            active_from=datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
            active_to=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
            source_updated_at=datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
        ),
    )
    content = json.dumps(
        [
            {
                "source_id": "ambiguous-revision-001",
                "store_id": "store-001",
                "item_id": "item-001",
                "sku_id": "sku-001",
                "metric_start": "2026-08-02T01:00:00Z",
                "bucket_granularity": "hour",
                "traffic_source": "recommend",
                "impressions": 1000,
                "clicks": 80,
                "data_as_of": "2026-08-02T03:00:00Z",
            }
        ]
    )

    result = TrafficLabIngestionService(db).import_metrics(
        "tenant-a",
        connector_id="virtual_taobao",
        source_format="json",
        content=content,
    )

    assert result["accepted_rows"] == 0
    assert result["quarantined_rows"] == 1
    assert result["rejected_rows"] == 0
    assert result["quarantined"][0]["reason_code"] == "listing_revision_ambiguous"
    assert domain.list_metric_buckets("tenant-a", limit=100) == []
    quarantine = domain.list_metric_quarantine("tenant-a", limit=100)
    assert {row["reason_code"] for row in quarantine} == {
        "listing_revision_ambiguous"
    }


def test_wp1_metric_hash_stays_stable_when_import_identity_is_not_supplied(tmp_path) -> None:
    db = Database(tmp_path / "traffic-hash-compat.sqlite3")
    db.initialize()
    domain = _seed_revision(db)
    revision_id = domain.list_revisions("tenant-a", limit=1)[0]["id"]
    value = TrafficMetricBucketUpsert(
        listing_revision_id=revision_id,
        metric_start=datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
        metric_end=datetime(2026, 8, 1, 2, 0, tzinfo=UTC),
        bucket_granularity="hour",
        traffic_source="recommend",
        impressions=1000,
        clicks=80,
        visitors=75,
        favorites=8,
        cart_adds=5,
        orders=2,
        sales_amount="218.00",
        ad_spend="0",
        search_impressions=100,
        recommend_impressions=900,
        data_as_of=datetime(2026, 8, 1, 3, 0, tzinfo=UTC),
        source_id="legacy-wp1-metric-001",
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

    result = domain.ingest_metric_bucket("tenant-a", value)
    assert result["payload_hash"] == payload_digest(legacy_payload)


def test_import_rejects_explicit_revision_with_mismatched_listing_identity(tmp_path) -> None:
    db = Database(tmp_path / "traffic-identity-mismatch.sqlite3")
    db.initialize()
    domain = _seed_revision(db)
    revision_id = domain.list_revisions("tenant-a", limit=1)[0]["id"]
    content = json.dumps(
        [
            {
                "source_id": "identity-mismatch-001",
                "listing_revision_id": revision_id,
                "store_id": "store-001",
                "item_id": "item-001",
                "sku_id": "different-sku",
                "metric_start": "2026-08-01T01:00:00Z",
                "bucket_granularity": "hour",
                "traffic_source": "recommend",
                "impressions": 1000,
                "clicks": 80,
                "data_as_of": "2026-08-01T03:00:00Z",
            }
        ]
    )

    result = TrafficLabIngestionService(db).import_metrics(
        "tenant-a",
        connector_id="virtual_taobao",
        source_format="json",
        content=content,
    )
    assert result["accepted_rows"] == 0
    assert result["quarantined_rows"] == 0
    assert result["rejected_rows"] == 1
    assert result["rejected"][0]["reason"] == "listing_revision_identity_mismatch"
    assert domain.list_metric_buckets("tenant-a", limit=100) == []


def test_virtual_connector_resources_sync_with_stable_change_receipts(tmp_path) -> None:
    from ecommerce_agent.business.service import OperationsService

    db = Database(tmp_path / "virtual-traffic-sync.sqlite3")
    db.initialize()
    operations = OperationsService(db)
    capabilities = operations.connectors.get("virtual_taobao").capabilities()
    assert {"listing_revision", "traffic_metrics"} <= set(capabilities.resources)

    revisions = operations.sync(
        tenant_id="tenant-a",
        connector_id="virtual_taobao",
        resource="listing_revision",
        limit=500,
    )
    metrics = operations.sync(
        tenant_id="tenant-a",
        connector_id="virtual_taobao",
        resource="traffic_metrics",
        limit=500,
    )
    assert revisions["items_received"] == revisions["items_applied"]
    assert revisions["items_received"] >= 3
    assert len(revisions["receipts"]) == revisions["items_received"]
    assert {item["status"] for item in revisions["receipts"]} == {"confirmed"}
    assert metrics["items_received"] == metrics["items_applied"]
    assert metrics["items_received"] >= 54
    assert metrics["items_quarantined"] == 0

    revision_replay = operations.sync(
        tenant_id="tenant-a",
        connector_id="virtual_taobao",
        resource="listing_revision",
        limit=500,
    )
    metric_replay = operations.sync(
        tenant_id="tenant-a",
        connector_id="virtual_taobao",
        resource="traffic_metrics",
        limit=500,
    )
    assert revision_replay["items_applied"] == 0
    assert revision_replay["items_idempotent"] == revisions["items_received"]
    assert revision_replay["receipts"] == revisions["receipts"]
    assert metric_replay["items_applied"] == 0
    assert metric_replay["items_idempotent"] == metrics["items_received"]
    assert len(TrafficLabService(db).list_metric_buckets("tenant-a", limit=100)) == (
        metrics["items_received"]
    )


def test_virtual_traffic_fixture_is_replayable_noisy_and_hides_policy() -> None:
    first = VirtualTaobaoConnector()
    second = VirtualTaobaoConnector()
    revisions = first.pull(PullRequest(resource="listing_revision", limit=500))
    metrics = first.pull(PullRequest(resource="traffic_metrics", limit=500))
    replay = second.pull(PullRequest(resource="traffic_metrics", limit=500))
    assert metrics.model_dump(mode="json") == replay.model_dump(mode="json")
    assert len(metrics.records) >= 54

    serialized = json.dumps(metrics.model_dump(mode="json"), sort_keys=True)
    assert "ground_truth" not in serialized
    assert "expected_direction" not in serialized
    assert "policy_weight" not in serialized
    assert not hasattr(first, "ground_truth")
    assert not hasattr(first, "policy")

    windows = sorted(
        (
            datetime.fromisoformat(record.payload["active_from"]),
            datetime.fromisoformat(record.payload["active_to"]),
            int(record.payload["revision_no"]),
        )
        for record in revisions.records
    )
    grouped: dict[int, list[dict[str, object]]] = {
        revision_no: [] for _, _, revision_no in windows
    }
    for record in metrics.records:
        metric_start = datetime.fromisoformat(record.payload["metric_start"])
        revision_no = next(
            revision_no
            for start, end, revision_no in windows
            if start <= metric_start < end
        )
        grouped[revision_no].append(record.payload)

    assert {1, 2} <= set(grouped)

    def ctr(rows: list[dict[str, object]]) -> float:
        return sum(int(row["clicks"]) for row in rows) / sum(
            int(row["impressions"]) for row in rows
        )

    assert ctr(grouped[2]) > ctr(grouped[1]) + 0.015
    assert statistics.pstdev(int(row["impressions"]) for row in grouped[1]) > 20
    assert len(
        {
            round(
                int(row.payload["clicks"]) / int(row.payload["impressions"]),
                4,
            )
            for row in metrics.records
        }
    ) > 10
    assert statistics.mean(
        int(row["recommend_impressions"]) for row in grouped[2][-12:]
    ) > statistics.mean(
        int(row["recommend_impressions"]) for row in grouped[1][-12:]
    )


def test_virtual_traffic_fixture_contains_observable_stockout_penalty() -> None:
    connector = VirtualTaobaoConnector()
    revisions = connector.pull(PullRequest(resource="listing_revision", limit=500))
    metrics = connector.pull(PullRequest(resource="traffic_metrics", limit=500))
    stockout_revisions = [
        record
        for record in revisions.records
        if record.payload["attributes"]["stock_status"] == "out_of_stock"
    ]

    assert stockout_revisions
    stockout = stockout_revisions[0].payload
    stockout_start = datetime.fromisoformat(stockout["active_from"])
    stockout_end = datetime.fromisoformat(stockout["active_to"])
    treatment = next(
        record.payload
        for record in revisions.records
        if int(record.payload["revision_no"]) == 2
    )
    treatment_start = datetime.fromisoformat(treatment["active_from"])
    treatment_end = datetime.fromisoformat(treatment["active_to"])
    stockout_rows = [
        record.payload
        for record in metrics.records
        if stockout_start
        <= datetime.fromisoformat(record.payload["metric_start"])
        < stockout_end
    ]
    in_stock_treatment_rows = [
        record.payload
        for record in metrics.records
        if treatment_start
        <= datetime.fromisoformat(record.payload["metric_start"])
        < treatment_end
    ]

    assert stockout_rows
    assert statistics.mean(int(row["impressions"]) for row in stockout_rows) < (
        statistics.mean(int(row["impressions"]) for row in in_stock_treatment_rows)
        * 0.4
    )
    assert len({int(row["impressions"]) for row in stockout_rows}) > 1
