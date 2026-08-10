from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.auth import AdminOperatorCreateRequest
from ecommerce_agent.traffic_lab import CURRENT_FEATURE_SCHEMA_VERSION

from conftest import make_settings


ADMIN = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
OTHER_ADMIN = {
    "X-Admin-Id": "traffic-other-admin",
    "X-Admin-Key": "traffic-other-key-123456",
}


def _ok(client: TestClient, method: str, path: str, **kwargs):
    response = client.request(method, path, headers=ADMIN, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def test_traffic_lab_http_api_freezes_workflow_and_reads_persisted_insights(
    tmp_path,
) -> None:
    app = create_app(replace(make_settings(tmp_path), max_request_body_bytes=32_768))
    expected_paths = {
        "/v1/traffic-lab/assets",
        "/v1/traffic-lab/revisions",
        "/v1/traffic-lab/metrics/import",
        "/v1/traffic-lab/experiments",
        "/v1/traffic-lab/experiments/{experiment_id}/windows",
        "/v1/traffic-lab/experiments/{experiment_id}",
        "/v1/traffic-lab/experiments/{experiment_id}/analyze",
        "/v1/traffic-lab/experiments/{experiment_id}/analysis",
        "/v1/traffic-lab/items/{sku_id}/insights",
        "/v1/traffic-lab/hypotheses",
    }
    assert expected_paths <= set(app.openapi()["paths"])

    with TestClient(app) as client:
        asset = _ok(
            client,
            "POST",
            "/v1/traffic-lab/assets",
            json={
                "sha256": "a" * 64,
                "mime_type": "image/png",
                "width": 1200,
                "height": 1200,
                "storage_ref": "objects/traffic-lab/api.png",
                "feature_schema_version": CURRENT_FEATURE_SCHEMA_VERSION,
            },
        )
        revision = _ok(
            client,
            "POST",
            "/v1/traffic-lab/revisions",
            json={
                "connector_id": "traffic-api-fixture",
                "store_id": "traffic-store",
                "item_id": "traffic-item",
                "sku_id": "traffic-sku",
                "revision_no": 1,
                "title": "循环扇标准版",
                "main_image_asset_id": asset["asset_id"],
                "sale_price": "109.00",
                "attributes": {"stock_status": "in_stock"},
                "active_from": "2026-08-01T00:00:00Z",
                "active_to": "2026-08-02T00:00:00Z",
                "source_updated_at": "2026-08-01T00:00:00Z",
            },
        )
        revisions = _ok(
            client,
            "GET",
            "/v1/traffic-lab/revisions",
            params={"store_id": "traffic-store", "sku_id": "traffic-sku"},
        )
        assert revision["id"] in {item["id"] for item in revisions}

        metric = {
            "listing_revision_id": revision["id"],
            "metric_start": "2026-08-01T01:00:00Z",
            "bucket_granularity": "hour",
            "traffic_source": "recommend",
            "impressions": 1000,
            "clicks": 50,
            "source_id": "traffic-api-metric-001",
        }
        imported = _ok(
            client,
            "POST",
            "/v1/traffic-lab/metrics/import",
            json={
                "connector_id": "traffic-api-fixture",
                "source_format": "json",
                "content": json.dumps([metric]),
                "source_timezone": "UTC",
            },
        )
        assert imported["accepted_rows"] == 1

        experiment = _ok(
            client,
            "POST",
            "/v1/traffic-lab/experiments",
            json={
                "store_id": "traffic-store",
                "sku_id": "traffic-sku",
                "experiment_type": "aa",
                "primary_metric": "ctr",
                "started_at": "2026-08-01T00:00:00Z",
                "ended_at": "2026-08-02T00:00:00Z",
                "control_revision_id": revision["id"],
                "treatment_revision_id": revision["id"],
                "minimum_exposure": 100,
                "washout_window": 0,
                "analysis_policy_version": "traffic-analysis-v2",
            },
        )
        experiment_id = experiment["experiment_id"]
        _ok(
            client,
            "POST",
            f"/v1/traffic-lab/experiments/{experiment_id}/windows",
            json={
                "listing_revision_id": revision["id"],
                "window_start": "2026-08-01T00:00:00Z",
                "window_end": "2026-08-02T00:00:00Z",
                "assignment": "control",
                "source_receipt_id": "traffic-api-window-001",
            },
        )
        detail = _ok(client, "GET", f"/v1/traffic-lab/experiments/{experiment_id}")
        assert detail["window_quality"]["window_count"] == len(detail["windows"]) == 1

        analyzed = _ok(
            client, "POST", f"/v1/traffic-lab/experiments/{experiment_id}/analyze"
        )
        assert analyzed["evidence"]["statistics_authority"] == "deterministic_code"
        run_id = analyzed["analysis_run_id"]
        runs = _ok(
            client, "GET", f"/v1/traffic-lab/experiments/{experiment_id}/analysis"
        )
        assert runs[0]["analysis_run_id"] == run_id

        insights = _ok(
            client,
            "GET",
            "/v1/traffic-lab/items/traffic-sku/insights",
            params={"store_id": "traffic-store"},
        )
        assert insights["evidence_source"] == "traffic_analysis_runs"
        assert insights["statistics_recomputed"] is False
        assert insights["platform_weight_claim"] is False
        assert insights["insights"][0]["analysis"]["analysis_run_id"] == run_id
        hypotheses = _ok(
            client,
            "POST",
            "/v1/traffic-lab/hypotheses",
            json={"analysis_run_id": run_id},
        )
        assert hypotheses["analysis_run_id"] == run_id
        assert hypotheses["statistics_recomputed"] is False
        assert hypotheses["platform_weight_claim"] is False


def test_traffic_lab_http_api_enforces_admin_tenant_scope(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    app.state.agent.auth.create_admin_operator(
        "traffic-other-tenant",
        AdminOperatorCreateRequest(
            admin_id="traffic-other-admin",
            name="Traffic other tenant",
            key="traffic-other-key-123456",
        ),
        "admin-test",
    )
    with TestClient(app) as client:
        _ok(
            client,
            "POST",
            "/v1/traffic-lab/assets",
            json={
                "sha256": "c" * 64,
                "mime_type": "image/png",
                "width": 1,
                "height": 1,
                "storage_ref": "objects/traffic-lab/scope.png",
                "feature_schema_version": CURRENT_FEATURE_SCHEMA_VERSION,
            },
        )
        revisions = client.get("/v1/traffic-lab/revisions", headers=OTHER_ADMIN)
        assert revisions.status_code == 200
        assert revisions.json() == []
        hidden = client.get(
            "/v1/traffic-lab/experiments/not-owned", headers=OTHER_ADMIN
        )
        assert hidden.status_code == 404
