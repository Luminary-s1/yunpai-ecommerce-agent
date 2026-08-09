from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any


def build_virtual_traffic_records() -> dict[str, list[dict[str, Any]]]:
    """Build replayable observations without returning the simulator's hidden inputs."""

    start = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
    boundary = start + timedelta(hours=24)
    end = start + timedelta(hours=48)
    stockout_end = end + timedelta(hours=6)
    revisions = [
        {
            "source_id": "virtual-listing-revision-001",
            "source_version": start.isoformat(),
            "occurred_at": start.isoformat(),
            "payload": {
                "store_id": "virtual-shop-001",
                "item_id": "YP-SPU-TRAFFIC-001",
                "sku_id": "YP-SKU-TRAFFIC-001",
                "revision_no": 1,
                "title": "云湃空气循环扇 标准静音版",
                "sale_price": "109.00",
                "attributes": {
                    "stock_status": "in_stock",
                    "campaign": None,
                    "ad_plan": "fixed-baseline",
                },
                "active_from": start.isoformat(),
                "active_to": boundary.isoformat(),
                "source_receipt_id": "virtual-listing-receipt-001",
                "applied_at": start.isoformat(),
                "asset": {
                    "sha256": "a" * 64,
                    "mime_type": "image/png",
                    "width": 1200,
                    "height": 1200,
                    "storage_ref": "objects/traffic-lab/virtual-control.png",
                    "source_ref": "fixture://virtual-traffic/control",
                    "feature_schema_version": "image-v1",
                },
            },
        },
        {
            "source_id": "virtual-listing-revision-002",
            "source_version": boundary.isoformat(),
            "occurred_at": boundary.isoformat(),
            "payload": {
                "store_id": "virtual-shop-001",
                "item_id": "YP-SPU-TRAFFIC-001",
                "sku_id": "YP-SKU-TRAFFIC-001",
                "revision_no": 2,
                "title": "云湃空气循环扇 强劲静音 远距送风",
                "sale_price": "109.00",
                "attributes": {
                    "stock_status": "in_stock",
                    "campaign": None,
                    "ad_plan": "fixed-baseline",
                },
                "active_from": boundary.isoformat(),
                "active_to": end.isoformat(),
                "source_receipt_id": "virtual-listing-receipt-002",
                "applied_at": boundary.isoformat(),
                "asset": {
                    "sha256": "b" * 64,
                    "mime_type": "image/png",
                    "width": 1200,
                    "height": 1200,
                    "storage_ref": "objects/traffic-lab/virtual-treatment.png",
                    "source_ref": "fixture://virtual-traffic/treatment",
                    "feature_schema_version": "image-v1",
                },
            },
        },
        {
            "source_id": "virtual-listing-revision-003",
            "source_version": end.isoformat(),
            "occurred_at": end.isoformat(),
            "payload": {
                "store_id": "virtual-shop-001",
                "item_id": "YP-SPU-TRAFFIC-001",
                "sku_id": "YP-SKU-TRAFFIC-001",
                "revision_no": 3,
                "title": "云湃空气循环扇 强劲静音 远距送风",
                "sale_price": "109.00",
                "attributes": {
                    "stock_status": "out_of_stock",
                    "campaign": None,
                    "ad_plan": "fixed-baseline",
                },
                "active_from": end.isoformat(),
                "active_to": stockout_end.isoformat(),
                "source_receipt_id": "virtual-listing-receipt-003",
                "applied_at": end.isoformat(),
                "asset": {
                    "sha256": "b" * 64,
                    "mime_type": "image/png",
                    "width": 1200,
                    "height": 1200,
                    "storage_ref": "objects/traffic-lab/virtual-treatment.png",
                    "source_ref": "fixture://virtual-traffic/treatment",
                    "feature_schema_version": "image-v1",
                },
            },
        },
    ]

    rng = random.Random(20260809)
    metrics: list[dict[str, Any]] = []
    base_hourly_exposure = 920
    baseline_click_rate = 0.052
    creative_click_lift = 0.032
    baseline_conversion_rate = 0.022
    creative_conversion_lift = 0.004
    stockout_exposure_multiplier = 0.2
    for hour_index in range(54):
        metric_start = start + timedelta(hours=hour_index)
        metric_end = metric_start + timedelta(hours=1)
        changed = hour_index >= 24
        stock_available = hour_index < 48
        phase_hour = hour_index - 24 if changed else hour_index
        daily_cycle = (phase_hour % 12) * 13 - 70
        recent = metrics[-4:]
        recent_impressions = sum(int(item["payload"]["impressions"]) for item in recent)
        recent_clicks = sum(int(item["payload"]["clicks"]) for item in recent)
        recent_orders = sum(int(item["payload"]["orders"]) for item in recent)
        recent_ctr = recent_clicks / recent_impressions if recent_impressions else 0.0
        recent_cvr = recent_orders / recent_clicks if recent_clicks else 0.0
        feedback_exposure = round(
            base_hourly_exposure
            * (
                max(0.0, recent_ctr - baseline_click_rate) * 5
                + max(0.0, recent_cvr - baseline_conversion_rate) * 3
            )
        )
        stock_multiplier = 1.0 if stock_available else stockout_exposure_multiplier
        impressions = max(
            100,
            round(
                (
                    base_hourly_exposure
                    + daily_cycle
                    + feedback_exposure
                    + rng.randint(-85, 85)
                )
                * stock_multiplier
            ),
        )
        click_rate = (
            baseline_click_rate
            + (creative_click_lift if changed else 0.0)
            + rng.uniform(-0.007, 0.007)
        )
        clicks = max(0, min(impressions, round(impressions * click_rate)))
        visitors = max(0, clicks - rng.randint(0, max(1, clicks // 12)))
        favorites = round(clicks * (0.07 + rng.uniform(-0.01, 0.01)))
        cart_adds = round(clicks * (0.055 + rng.uniform(-0.008, 0.008)))
        conversion_rate = (
            baseline_conversion_rate
            + (creative_conversion_lift if changed else 0.0)
            + rng.uniform(-0.004, 0.004)
        )
        orders = max(0, round(clicks * conversion_rate))
        search_impressions = round(impressions * (0.24 + rng.uniform(-0.02, 0.02)))
        recommend_impressions = impressions - search_impressions
        data_as_of = metric_end + timedelta(minutes=5)
        metrics.append(
            {
                "source_id": f"virtual-traffic-hour-{hour_index + 1:03d}",
                "source_version": data_as_of.isoformat(),
                "occurred_at": data_as_of.isoformat(),
                "payload": {
                    "store_id": "virtual-shop-001",
                    "item_id": "YP-SPU-TRAFFIC-001",
                    "sku_id": "YP-SKU-TRAFFIC-001",
                    "metric_start": metric_start.isoformat(),
                    "metric_end": metric_end.isoformat(),
                    "bucket_granularity": "hour",
                    "traffic_source": "recommend",
                    "impressions": impressions,
                    "clicks": clicks,
                    "visitors": visitors,
                    "favorites": favorites,
                    "cart_adds": cart_adds,
                    "orders": orders,
                    "sales_amount": str(Decimal(orders * 109).quantize(Decimal("0.01"))),
                    "ad_spend": "0.00",
                    "search_impressions": search_impressions,
                    "recommend_impressions": recommend_impressions,
                    "data_as_of": data_as_of.isoformat(),
                },
            }
        )
    return {"listing_revision": revisions, "traffic_metrics": metrics}
