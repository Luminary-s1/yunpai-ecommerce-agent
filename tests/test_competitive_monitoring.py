from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import time

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business import (
    CompetitiveAlertTransition,
    CompetitiveMonitorUpsert,
    CompetitorObservationCreate,
)
from ecommerce_agent.service import AgentService
from ecommerce_agent.tools import ToolExecutionContext

from conftest import make_settings


def observation(
    *,
    observed_at: datetime,
    competitor_price: str,
    source_type: str = "authorized_api",
    is_estimate: bool = False,
) -> CompetitorObservationCreate:
    return CompetitorObservationCreate(
        connector_id="licensed-feed",
        store_id="store-a",
        subject_sku="sku-a",
        competitor_name="竞店 A",
        competitor_sku="comp-a",
        subject_price=Decimal("100"),
        competitor_price=Decimal(competitor_price),
        currency="CNY",
        source_type=source_type,
        source_ref="https://licensed.example/evidence/a",
        is_estimate=is_estimate,
        observed_at=observed_at,
    )


def monitor_payload(
    *,
    expected_record_version: int = 0,
    include_estimates: bool = False,
    enabled: bool = True,
) -> CompetitiveMonitorUpsert:
    return CompetitiveMonitorUpsert(
        store_id="store-a",
        subject_sku="sku-a",
        enabled=enabled,
        undercut_threshold_percent=Decimal("5"),
        price_drop_threshold_percent=Decimal("10"),
        stale_after_hours=24,
        include_estimates=include_estimates,
        require_approved_match=False,
        expected_record_version=expected_record_version,
    )


def test_alert_lifecycle_is_idempotent_and_reopens_on_new_evidence(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    base = datetime.now(UTC).replace(microsecond=0)
    competitive = service.operations.competitive
    try:
        monitor = competitive.upsert_monitor(
            "tenant-test", monitor_payload(), actor="admin-test"
        )
        assert monitor["record_version"] == 1

        first = competitive.record(
            "tenant-test", observation(observed_at=base, competitor_price="80")
        )
        assert first["alert_evaluation"]["created"] == 1
        undercut = next(
            item for item in competitive.list_alerts("tenant-test")
            if item["alert_code"] == "competitor_undercut"
        )
        assert undercut["status"] == "open"
        assert undercut["occurrence_count"] == 1

        repeated = competitive.evaluate_monitor(
            "tenant-test", monitor["id"], now=base + timedelta(minutes=1)
        )
        unchanged = next(
            item for item in competitive.list_alerts("tenant-test")
            if item["alert_code"] == "competitor_undercut"
        )
        assert repeated["created"] == repeated["updated"] == 0
        assert unchanged["record_version"] == undercut["record_version"]
        assert unchanged["occurrence_count"] == 1

        acknowledged = competitive.transition_alert(
            "tenant-test",
            undercut["id"],
            CompetitiveAlertTransition(
                target_status="acknowledged",
                expected_record_version=undercut["record_version"],
                note="已交由定价负责人复核",
            ),
            actor="admin-test",
        )
        assert acknowledged["status"] == "acknowledged"
        with pytest.raises(ValueError, match="version_conflict"):
            competitive.transition_alert(
                "tenant-test",
                undercut["id"],
                CompetitiveAlertTransition(
                    target_status="resolved",
                    expected_record_version=undercut["record_version"],
                    note="使用过期版本提交",
                ),
                actor="admin-test",
            )

        competitive.record(
            "tenant-test",
            observation(observed_at=base + timedelta(minutes=2), competitor_price="70"),
        )
        reopened = next(
            item for item in competitive.list_alerts("tenant-test")
            if item["alert_code"] == "competitor_undercut"
        )
        assert reopened["status"] == "open"
        assert reopened["occurrence_count"] == 2
        assert reopened["acknowledged_by"] is None

        competitive.record(
            "tenant-test",
            observation(observed_at=base + timedelta(minutes=3), competitor_price="110"),
        )
        cleared = next(
            item for item in competitive.list_alerts("tenant-test")
            if item["alert_code"] == "competitor_undercut"
        )
        assert cleared["status"] == "resolved"
        assert cleared["resolution_note"] == "condition_cleared"

        competitive.record(
            "tenant-test",
            observation(observed_at=base + timedelta(minutes=4), competitor_price="75"),
        )
        recurred = next(
            item for item in competitive.list_alerts("tenant-test")
            if item["alert_code"] == "competitor_undercut"
        )
        assert recurred["status"] == "open"
        assert recurred["occurrence_count"] == 3
    finally:
        service.close()


def test_freshness_policy_excludes_estimates_and_update_requires_version(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    base = datetime(2026, 7, 19, 0, 0, tzinfo=UTC)
    try:
        monitor = competitive.upsert_monitor(
            "tenant-test", monitor_payload(), actor="admin-test"
        )
        competitive.record(
            "tenant-test",
            observation(
                observed_at=base,
                competitor_price="90",
                source_type="virtual",
                is_estimate=True,
            ),
        )
        missing = competitive.list_alerts("tenant-test")
        assert len(missing) == 1
        assert missing[0]["competitor_sku"] == "__monitor__"
        assert missing[0]["details"]["reason"] == "no_eligible_observation"

        with pytest.raises(ValueError, match="version_conflict"):
            competitive.upsert_monitor(
                "tenant-test",
                monitor_payload(expected_record_version=99, include_estimates=True),
                actor="admin-test",
            )
        changed = competitive.upsert_monitor(
            "tenant-test",
            monitor_payload(
                expected_record_version=monitor["record_version"], include_estimates=True
            ),
            actor="admin-test",
        )
        result = competitive.evaluate_monitor(
            "tenant-test", changed["id"], now=base + timedelta(hours=25)
        )
        alerts = competitive.list_alerts("tenant-test")
        real_stale = next(
            item for item in alerts
            if item["competitor_sku"] == "comp-a" and item["alert_code"] == "data_stale"
        )
        synthetic = next(item for item in alerts if item["competitor_sku"] == "__monitor__")
        assert changed["alert_evaluation"]["auto_resolved"] == 1
        assert result["auto_resolved"] == 0
        assert real_stale["alert_code"] == "data_stale"
        assert real_stale["status"] == "open"
        assert synthetic["status"] == "resolved"
    finally:
        service.close()


def test_monitor_evaluation_is_tenant_scoped_and_concurrency_idempotent(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    try:
        monitor = competitive.upsert_monitor(
            "tenant-test", monitor_payload(), actor="admin-test"
        )
        with pytest.raises(ValueError, match="not_found"):
            competitive.evaluate_monitor("tenant-other", monitor["id"])
        assert competitive.list_monitors("tenant-other") == []
        assert competitive.list_alerts("tenant-other") == []

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda _: competitive.evaluate_monitor("tenant-test", monitor["id"]),
                    range(12),
                )
            )
        alerts = competitive.list_alerts("tenant-test")
        assert sum(item["created"] for item in results) == 0
        assert len(alerts) == 1
        assert alerts[0]["occurrence_count"] == 1
        assert alerts[0]["record_version"] == 1
    finally:
        service.close()


def test_competitive_monitoring_api_and_agent_tool_return_evidence(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }
    with TestClient(app) as client:
        created = client.put(
            "/v1/competitive/monitors",
            headers=headers,
            json={
                "store_id": "store-a",
                "subject_sku": "sku-a",
                "undercut_threshold_percent": "5",
                "price_drop_threshold_percent": "10",
                "stale_after_hours": 24,
                "include_estimates": True,
                "require_approved_match": False,
                "expected_record_version": 0,
            },
        )
        assert created.status_code == 200
        monitor = created.json()

        recorded = client.post(
            "/v1/competitive/observations",
            headers=headers,
            json=observation(
                observed_at=datetime.now(UTC), competitor_price="80"
            ).model_dump(mode="json"),
        )
        assert recorded.status_code == 200
        assert recorded.json()["alert_evaluation"]["created"] == 1

        evaluated = client.post(
            f"/v1/competitive/monitors/{monitor['id']}/evaluate", headers=headers
        )
        assert evaluated.status_code == 200
        alerts_response = client.get(
            "/v1/competitive/alerts?status=open", headers=headers
        )
        assert alerts_response.status_code == 200
        alert = alerts_response.json()[0]
        acknowledged = client.post(
            f"/v1/competitive/alerts/{alert['id']}/transition",
            headers=headers,
            json={
                "target_status": "acknowledged",
                "expected_record_version": alert["record_version"],
                "note": "运营已确认并开始复核",
            },
        )
        assert acknowledged.status_code == 200
        assert acknowledged.json()["status"] == "acknowledged"
        conflict = client.post(
            f"/v1/competitive/alerts/{alert['id']}/transition",
            headers=headers,
            json={
                "target_status": "resolved",
                "expected_record_version": alert["record_version"],
                "note": "过期版本不能覆盖最新处置",
            },
        )
        assert conflict.status_code == 409

        service = app.state.agent
        context = ToolExecutionContext(
            tenant_id="tenant-test",
            client_id="client-test",
            session_id="session-test",
            trace_id="trace-test",
            trusted_context={},
        )
        spec, arguments = service.tools.validate_selection(
            name="get_competitor_price_analysis",
            arguments={"subject_sku": "sku-a", "store_id": "store-a"},
            requested_mode="observe",
            context=context,
        )
        result = service.tools.execute(spec=spec, arguments=arguments, context=context)
        assert result.status == "success"
        assert result.output["monitors"][0]["id"] == monitor["id"]
        assert result.output["alerts"] == []
        assert result.output["observations"] == []
        assert result.output["quality_gate"] == {
            "approved_match_required": True,
            "eligible_competitors": 0,
            "excluded_unverified_competitors": 1,
        }


def test_competitive_monitor_worker_runs_and_stops_with_service(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        competitive_monitor_worker_enabled=True,
        competitive_monitor_poll_seconds=0.02,
    )
    service = AgentService(settings)
    try:
        service.operations.competitive.upsert_monitor(
            "tenant-test", monitor_payload(), actor="admin-test"
        )
        service.start()
        deadline = time.monotonic() + 2
        while service.competitive_monitor_worker_status()["cycles"] < 1:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        status = service.competitive_monitor_worker_status()
        assert status["running"] is True
        assert status["evaluated"] >= 1
        assert status["last_error"] is None
        ready, detail = service.readiness()
        assert ready is True
        assert detail["checks"]["competitive_monitor_worker"] is True
    finally:
        service.close()
    assert service.competitive_monitor_worker_status()["running"] is False
