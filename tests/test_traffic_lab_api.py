from __future__ import annotations

import json
from dataclasses import replace

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.auth import AdminOperatorCreateRequest
from ecommerce_agent.business_calendar import StoreBusinessCalendarUpsert
from ecommerce_agent.tools import ToolExecutionContext
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


def _create_http_experiment(client: TestClient) -> dict:
    asset = _ok(
        client,
        "POST",
        "/v1/traffic-lab/assets",
        json={
            "sha256": "b" * 64,
            "mime_type": "image/png",
            "width": 1200,
            "height": 1200,
            "storage_ref": "objects/traffic-lab/lifecycle.png",
            "feature_schema_version": CURRENT_FEATURE_SCHEMA_VERSION,
        },
    )
    revision = _ok(
        client,
        "POST",
        "/v1/traffic-lab/revisions",
        json={
            "connector_id": "traffic-lifecycle-fixture",
            "store_id": "traffic-lifecycle-store",
            "item_id": "traffic-lifecycle-item",
            "sku_id": "traffic-lifecycle-sku",
            "revision_no": 1,
            "title": "生命周期测试商品",
            "main_image_asset_id": asset["asset_id"],
            "sale_price": "99.00",
            "attributes": {"stock_status": "in_stock"},
            "active_from": "2026-08-01T00:00:00Z",
            "active_to": "2026-08-03T00:00:00Z",
            "source_updated_at": "2026-08-01T00:00:00Z",
        },
    )
    return _ok(
        client,
        "POST",
        "/v1/traffic-lab/experiments",
        json={
            "store_id": "traffic-lifecycle-store",
            "sku_id": "traffic-lifecycle-sku",
            "experiment_type": "aa",
            "primary_metric": "ctr",
            "started_at": "2026-08-01T00:00:00Z",
            "control_revision_id": revision["id"],
            "treatment_revision_id": revision["id"],
            "minimum_exposure": 100,
            "washout_window": 0,
            "analysis_policy_version": "traffic-analysis-v2",
        },
    )


def _seed_http_calendar(app, store_id: str) -> None:
    app.state.agent.operations.business_calendars.upsert_calendar(
        "tenant-test",
        StoreBusinessCalendarUpsert(
            store_id=store_id,
            timezone="UTC",
            effective_from="2026-07-31T00:00:00Z",
            changed_by="traffic-api-test-fixture",
        ),
    )


def test_traffic_lab_http_api_exposes_safe_audited_experiment_lifecycle(
    tmp_path,
) -> None:
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
    _seed_http_calendar(app, "traffic-lifecycle-store")
    with TestClient(app) as client:
        experiment = _create_http_experiment(client)
        experiment_id = experiment["experiment_id"]
        transition_path = (
            f"/v1/traffic-lab/experiments/{experiment_id}/transition"
        )
        assert experiment["status"] == "draft"
        assert experiment["record_version"] == 1
        assert experiment["ended_at"] is None

        hidden = client.post(
            transition_path,
            headers=OTHER_ADMIN,
            json={"status": "ready", "expected_version": 1},
        )
        assert hidden.status_code == 404
        assert hidden.json()["detail"] == "traffic_experiment_not_found"

        missing_version = client.post(
            transition_path,
            headers=ADMIN,
            json={"status": "ready"},
        )
        assert missing_version.status_code == 422

        invalid = client.post(
            transition_path,
            headers=ADMIN,
            json={"status": "running", "expected_version": 1},
        )
        assert invalid.status_code == 409
        assert invalid.json()["detail"] == "invalid_experiment_transition"

        ready = _ok(
            client,
            "POST",
            transition_path,
            json={"status": "ready", "expected_version": 1},
        )
        assert (ready["status"], ready["record_version"]) == ("ready", 2)
        ready_detail = _ok(
            client, "GET", f"/v1/traffic-lab/experiments/{experiment_id}"
        )
        assert set(ready_detail["allowed_transitions"]) == {"running", "invalid"}

        stale = client.post(
            transition_path,
            headers=ADMIN,
            json={"status": "running", "expected_version": 1},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"] == "experiment_version_conflict"

        running = _ok(
            client,
            "POST",
            transition_path,
            json={"status": "running", "expected_version": 2},
        )
        assert (running["status"], running["record_version"]) == ("running", 3)

        missing_end = client.post(
            transition_path,
            headers=ADMIN,
            json={"status": "completed", "expected_version": 3},
        )
        assert missing_end.status_code == 409
        assert missing_end.json()["detail"] == "experiment_end_required"

        completed = _ok(
            client,
            "POST",
            transition_path,
            json={
                "status": "completed",
                "expected_version": 3,
                "ended_at": "2026-08-02T00:00:00Z",
            },
        )
        assert (completed["status"], completed["record_version"]) == (
            "completed",
            4,
        )
        assert completed["ended_at"] == "2026-08-02T00:00:00+00:00"

        detail = _ok(client, "GET", f"/v1/traffic-lab/experiments/{experiment_id}")
        assert detail["experiment"] == {
            key: value for key, value in completed.items() if key != "write_status"
        }

        events = _ok(
            client,
            "GET",
            "/v1/admin/audit",
            params={"event_type": "traffic_lab.experiment.transitioned"},
        )
        assert [event["detail"]["status"] for event in events] == [
            "completed",
            "running",
            "ready",
        ]
        assert [event["detail"]["record_version"] for event in events] == [4, 3, 2]
        assert [event["detail"]["expected_version"] for event in events] == [3, 2, 1]
        assert all(event["subject_id"] == experiment_id for event in events)
        assert all(event["actor"] == "admin-test" for event in events)


def test_traffic_lab_http_api_freezes_workflow_and_reads_persisted_insights(
    tmp_path,
) -> None:
    app = create_app(replace(make_settings(tmp_path), max_request_body_bytes=32_768))
    _seed_http_calendar(app, "traffic-store")
    expected_paths = {
        "/v1/traffic-lab/assets",
        "/v1/traffic-lab/revisions",
        "/v1/traffic-lab/metrics/import",
        "/v1/traffic-lab/experiments",
        "/v1/traffic-lab/experiments/{experiment_id}/transition",
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
                "attributes": {
                    "category": "home-appliance",
                    "stock_status": "in_stock",
                    "campaign": "none",
                    "ad_plan": "none",
                    "holiday_calendar_version": "cn-2026-v1",
                    "store_traffic_baseline_version": "baseline-2026-08",
                    "historical_ctr": "0.05",
                    "historical_cvr": "0.10",
                },
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

        metrics = [
            {
                "listing_revision_id": revision["id"],
                "metric_start": f"2026-08-01T{hour:02d}:00:00Z",
                "bucket_granularity": "hour",
                "traffic_source": "recommend",
                "impressions": 1000,
                "clicks": 50,
                "recommend_impressions": 700 + hour * 10,
                "source_id": f"traffic-api-metric-{hour:03d}",
            }
            for hour in range(8)
        ]
        imported = _ok(
            client,
            "POST",
            "/v1/traffic-lab/metrics/import",
            json={
                "connector_id": "traffic-api-fixture",
                "source_format": "json",
                "content": json.dumps(metrics),
                "source_timezone": "UTC",
            },
        )
        assert imported["accepted_rows"] == 8

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
                "control_revision_id": revision["id"],
                "treatment_revision_id": revision["id"],
                "minimum_exposure": 100,
                "washout_window": 0,
                "analysis_policy_version": "traffic-analysis-v2",
            },
        )
        experiment_id = experiment["experiment_id"]
        for status, expected_version in (("ready", 1), ("running", 2)):
            transitioned = _ok(
                client,
                "POST",
                f"/v1/traffic-lab/experiments/{experiment_id}/transition",
                json={"status": status, "expected_version": expected_version},
            )
            assert transitioned["status"] == status
        _ok(
            client,
            "POST",
            f"/v1/traffic-lab/experiments/{experiment_id}/windows",
            json={
                "listing_revision_id": revision["id"],
                "window_start": "2026-08-01T00:00:00Z",
                "window_end": "2026-08-01T04:00:00Z",
                "assignment": "control",
                "source_receipt_id": "traffic-api-window-001",
            },
        )
        _ok(
            client,
            "POST",
            f"/v1/traffic-lab/experiments/{experiment_id}/windows",
            json={
                "listing_revision_id": revision["id"],
                "window_start": "2026-08-01T04:00:00Z",
                "window_end": "2026-08-01T08:00:00Z",
                "assignment": "treatment",
                "source_receipt_id": "traffic-api-window-002",
            },
        )
        completed = _ok(
            client,
            "POST",
            f"/v1/traffic-lab/experiments/{experiment_id}/transition",
            json={
                "status": "completed",
                "expected_version": 3,
                "ended_at": "2026-08-01T08:00:00Z",
            },
        )
        assert completed["status"] == "completed"
        detail = _ok(client, "GET", f"/v1/traffic-lab/experiments/{experiment_id}")
        assert detail["window_quality"]["window_count"] == len(detail["windows"]) == 2
        assert detail["allowed_transitions"] == []

        analyzed = _ok(
            client, "POST", f"/v1/traffic-lab/experiments/{experiment_id}/analyze"
        )
        assert analyzed["evidence"]["statistics_authority"] == "deterministic_code"
        assert analyzed["evidence"]["quality_gate"] == {
            "status": "passed",
            "quality": "valid",
            "strong_conclusion_allowed": False,
            "issues": [],
        }
        assert analyzed["evidence"]["statistical_conclusion"] == "no_detectable_effect"
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
        assert insights["freshness"]["status"] == "current"
        assert insights["freshness"]["usable_as_current"] is True
        hypotheses = _ok(
            client,
            "POST",
            "/v1/traffic-lab/hypotheses",
            json={"analysis_run_id": run_id},
        )
        assert hypotheses["analysis_run_id"] == run_id
        assert hypotheses["statistics_recomputed"] is False
        assert hypotheses["platform_weight_claim"] is False

        correction = dict(metrics[0])
        correction.update(clicks=55, data_as_of="2026-08-01T09:00:00Z")
        corrected = _ok(
            client,
            "POST",
            "/v1/traffic-lab/metrics/import",
            json={
                "connector_id": "traffic-api-fixture",
                "source_format": "json",
                "content": json.dumps([correction]),
                "source_timezone": "UTC",
            },
        )
        assert corrected["accepted_rows"] == 1
        stale_insights = _ok(
            client,
            "GET",
            "/v1/traffic-lab/items/traffic-sku/insights",
            params={"store_id": "traffic-store"},
        )
        stale_analysis = stale_insights["insights"][0]["analysis"]
        assert stale_analysis["analysis_run_id"] == run_id
        assert stale_analysis["effect_estimate"] == analyzed["effect_estimate"]
        assert stale_insights["statistics_recomputed"] is False
        assert stale_insights["freshness"]["status"] == "stale"
        assert stale_insights["freshness"]["usable_as_current"] is False
        assert "traffic_metric_evidence_changed" in stale_insights["freshness"][
            "reason_codes"
        ]

        context = ToolExecutionContext(
            tenant_id="tenant-test",
            client_id="client-test",
            session_id="traffic-freshness-session",
            trace_id="traffic-freshness-trace",
            trusted_context={"store_id": "traffic-store"},
        )
        spec, arguments = app.state.agent.tools.validate_selection(
            name="get_listing_traffic_insights",
            arguments={"store_id": "traffic-store", "sku_id": "traffic-sku"},
            requested_mode="observe",
            context=context,
        )
        tool_result = app.state.agent.tools.execute(
            spec=spec, arguments=arguments, context=context
        )
        assert tool_result.output["insights"][0]["analysis"]["analysis_run_id"] == run_id
        assert tool_result.output["freshness"] == stale_insights["freshness"]


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
