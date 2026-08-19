"""M9-R WP2 桥接层测试：EvidenceBridge 统一只读证据查询（WP5 验收修复版）。

用真实 TrafficLabService + tmp_path DB 种数据（asset/revision/bucket/experiment/
analysis run），验证 bridge 从真实持久化位置取证据，不再恒 missing/硬编码 actual。
对齐验收标准：条目 1（桥接 revision/experiment/analysis）、条目 2（freshness/provenance）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ecommerce_agent.business_calendar import StoreBusinessCalendarUpsert
from ecommerce_agent.database import Database
from ecommerce_agent.product_diagnosis.bridge import EvidenceBridge
from ecommerce_agent.traffic_lab import (
    CreativeAssetCreate,
    ListingRevisionCreate,
    TrafficExperimentCreate,
    TrafficLabService,
    TrafficMetricBucketUpsert,
)

BASE_TIME = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def _asset(**changes) -> CreativeAssetCreate:
    payload = {
        "sha256": "a" * 64,
        "mime_type": "image/png",
        "width": 1200,
        "height": 1200,
        "storage_ref": "objects/traffic-lab/a.png",
        "source_ref": "fixture://asset-a",
        "feature_schema_version": "image-v1",
    }
    payload.update(changes)
    return CreativeAssetCreate.model_validate(payload)


def _revision(asset_id: str, **changes) -> ListingRevisionCreate:
    payload = {
        "connector_id": "virtual_taobao",
        "store_id": "store-001",
        "item_id": "item-001",
        "sku_id": "sku-001",
        "revision_no": 1,
        "title": "测试商品 标题 A",
        "main_image_asset_id": asset_id,
        "sale_price": "109.00",
        "attributes": {"stock_status": "in_stock", "campaign": None},
        "active_from": BASE_TIME,
        "active_to": BASE_TIME + timedelta(hours=4),
        "source_updated_at": BASE_TIME,
    }
    payload.update(changes)
    return ListingRevisionCreate.model_validate(payload)


def _bucket(revision_id: str, **changes) -> TrafficMetricBucketUpsert:
    payload = {
        "listing_revision_id": revision_id,
        "connector_id": "virtual_taobao",
        "metric_start": BASE_TIME + timedelta(hours=1),
        "metric_end": BASE_TIME + timedelta(hours=2),
        "bucket_granularity": "hour",
        "traffic_source": "recommend",
        "impressions": 1000,
        "clicks": 80,
        "visitors": 75,
        "favorites": 8,
        "cart_adds": 5,
        "orders": 2,
        "sales_amount": "218.00",
        "ad_spend": "0",
        "search_impressions": 100,
        "recommend_impressions": 900,
        "data_as_of": BASE_TIME + timedelta(hours=3),
        "source_id": "metric-source-001",
    }
    payload.update(changes)
    return TrafficMetricBucketUpsert.model_validate(payload)


def _seeded_service(db: Database) -> TrafficLabService:
    """用真实 TrafficLabService 种 data：asset → revision → bucket → experiment → window。"""
    service = TrafficLabService(db)
    service.business_calendars.upsert_calendar(
        "tenant-a",
        StoreBusinessCalendarUpsert(
            store_id="store-001",
            timezone="Asia/Shanghai",
            effective_from=BASE_TIME - timedelta(days=1),
            changed_by="traffic-test-fixture",
        ),
    )
    created_asset = service.register_asset("tenant-a", _asset())
    control = service.create_revision(
        "tenant-a", _revision(created_asset["asset_id"], revision_no=1)
    )
    treatment = service.create_revision(
        "tenant-a", _revision(created_asset["asset_id"], revision_no=2)
    )
    service.upsert_metric_bucket(
        "tenant-a", _bucket(control["id"], source_id="metric-control")
    )
    created_experiment = service.create_experiment(
        "tenant-a",
        TrafficExperimentCreate(
            store_id="store-001",
            sku_id="sku-001",
            experiment_type="switchback",
            primary_metric="ctr",
            started_at=BASE_TIME,
            ended_at=BASE_TIME + timedelta(hours=4),
            control_revision_id=control["id"],
            treatment_revision_id=treatment["id"],
            minimum_exposure=1000,
            washout_window=15,
            analysis_policy_version="traffic-analysis-v2",
        ),
    )
    service.transition_experiment(
        "tenant-a",
        created_experiment["experiment_id"],
        __import__("ecommerce_agent.traffic_lab", fromlist=["TrafficExperimentTransition"])
        .TrafficExperimentTransition(status="ready"),
    )
    service.transition_experiment(
        "tenant-a",
        created_experiment["experiment_id"],
        __import__("ecommerce_agent.traffic_lab", fromlist=["TrafficExperimentTransition"])
        .TrafficExperimentTransition(status="running"),
    )
    service.add_experiment_window(
        "tenant-a",
        created_experiment["experiment_id"],
        __import__(
            "ecommerce_agent.traffic_lab", fromlist=["TrafficExperimentWindowCreate"]
        ).TrafficExperimentWindowCreate(
            listing_revision_id=control["id"],
            window_start=BASE_TIME,
            window_end=BASE_TIME + timedelta(hours=1),
            assignment="control",
            washout=False,
            source_receipt_id="receipt-001",
        ),
    )
    return service


def test_revision_view_reads_real_bucket_evidence(tmp_path) -> None:
    """revision 证据从真实 metric buckets 读取（不再恒 missing）。"""
    db = Database(tmp_path / "bridge.sqlite3")
    db.initialize()
    service = _seeded_service(db)
    bridge = EvidenceBridge(service)
    # 取挂过 metric bucket 的那个 revision（_seeded_service 里 control 挂了 bucket）
    with db.connect() as conn:
        bucket_rev = conn.execute(
            "SELECT listing_revision_id FROM traffic_metric_buckets WHERE tenant_id='tenant-a' LIMIT 1"
        ).fetchone()["listing_revision_id"]
    view = bridge.get_revision_view("tenant-a", bucket_rev)
    assert view["evidence_state"] == "demo"  # virtual_taobao connector → demo
    assert view["source_provenance"]["source_type"] == "virtual"
    assert view["data_as_of"] is not None  # 从 bucket 取到
    assert view["bucket_count"] >= 1


def test_missing_revision_view_explicit_missing(tmp_path) -> None:
    """revision 不存在 → 显式 missing（不静默）。"""
    db = Database(tmp_path / "bridge-missing.sqlite3")
    db.initialize()
    service = _seeded_service(db)
    bridge = EvidenceBridge(service)
    view = bridge.get_revision_view("tenant-a", "no-such-rev")
    assert view["evidence_state"] == "missing"
    assert "not_found" in view["reason"]


def test_revision_without_buckets_is_missing(tmp_path) -> None:
    """revision 存在但无 metric buckets → missing（真实数据不足，不冒充证据）。"""
    db = Database(tmp_path / "bridge-nobucket.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    service.business_calendars.upsert_calendar(
        "tenant-a",
        StoreBusinessCalendarUpsert(
            store_id="store-001",
            timezone="Asia/Shanghai",
            effective_from=BASE_TIME - timedelta(days=1),
            changed_by="traffic-test-fixture",
        ),
    )
    created_asset = service.register_asset("tenant-a", _asset())
    created = service.create_revision(
        "tenant-a", _revision(created_asset["asset_id"], revision_no=1)
    )
    bridge = EvidenceBridge(service)
    view = bridge.get_revision_view("tenant-a", created["id"])
    assert view["evidence_state"] == "missing"
    assert view["reason"] == "traffic_metric_evidence_not_found"


def test_experiment_without_analysis_is_missing(tmp_path) -> None:
    """experiment 存在但无 analysis run → missing（虚拟 run 不冒充实际）。"""
    db = Database(tmp_path / "bridge-noanalysis.sqlite3")
    db.initialize()
    service = TrafficLabService(db)
    service.business_calendars.upsert_calendar(
        "tenant-a",
        StoreBusinessCalendarUpsert(
            store_id="store-001",
            timezone="Asia/Shanghai",
            effective_from=BASE_TIME - timedelta(days=1),
            changed_by="traffic-test-fixture",
        ),
    )
    created_asset = service.register_asset("tenant-a", _asset())
    control = service.create_revision(
        "tenant-a", _revision(created_asset["asset_id"], revision_no=1)
    )
    treatment = service.create_revision(
        "tenant-a", _revision(created_asset["asset_id"], revision_no=2)
    )
    created_experiment = service.create_experiment(
        "tenant-a",
        TrafficExperimentCreate(
            store_id="store-001",
            sku_id="sku-001",
            experiment_type="switchback",
            primary_metric="ctr",
            started_at=BASE_TIME,
            ended_at=BASE_TIME + timedelta(hours=4),
            control_revision_id=control["id"],
            treatment_revision_id=treatment["id"],
            minimum_exposure=1000,
            washout_window=15,
            analysis_policy_version="traffic-analysis-v2",
        ),
    )
    bridge = EvidenceBridge(service)
    view = bridge.get_experiment_view(
        "tenant-a", created_experiment["experiment_id"]
    )
    assert view["evidence_state"] == "missing"
    assert view["reason"] == "traffic_analysis_evidence_not_found"


def test_gates_consume_quality_gate(tmp_path) -> None:
    """GateEngine 消费 quality_gate：blocked 时 all_passed=False。"""
    from ecommerce_agent.product_diagnosis.gates import GateEngine

    engine = GateEngine()
    blocked_view = {
        "evidence_state": "actual",
        "freshness": {"usable_as_current": True},
        "quality_gate": {"status": "blocked", "issues": ["aa_gate_missing"]},
    }
    passed, results = engine.run_all(blocked_view)
    assert passed is False
    assert any(r.name == "quality_gate" and not r.passed for r in results)
