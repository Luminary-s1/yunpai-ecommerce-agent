from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.auth import AdminOperatorCreateRequest
from ecommerce_agent.business.registry import business_module_catalog
from ecommerce_agent.service import AgentService
from ecommerce_agent.tools import ToolExecutionContext

from conftest import make_settings


ADMIN = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
OTHER_ADMIN = {
    "X-Admin-Id": "forecast-other-admin",
    "X-Admin-Key": "forecast-other-key-123456",
}
STORE = "virtual-shop-001"
SKU = "YP-SKU-001"
POLICY_UPDATE = {
    "store_id": STORE,
    "forecast_policy": {
        "policy_version": "forecast-wp4-v1",
        "horizons": [7, 14, 30],
        "minimum_history_days": 14,
        "backtest_windows": 4,
        "required_relative_improvement": 0.02,
        "interval_levels": [0.5, 0.8, 0.95],
        "candidate_models": [
            "last_value",
            "seasonal_naive_7",
            "rolling_mean",
            "weighted_moving_average",
            "ewma",
            "croston",
            "tsb",
        ],
    },
    "inventory_policy": {
        "supplier_lead_days": 7,
        "review_period_days": 7,
        "service_level": "0.80",
        "minimum_order_qty": "6",
        "order_multiple": "6",
        "minimum_safety_stock": "3",
        "maximum_stock_days": 30,
        "policy_version": "inventory-wp4-v1",
    },
}


def _prepare_virtual_inputs(service: AgentService) -> None:
    for resource in ("orders", "inventory"):
        result = service.operations.sync(
            tenant_id="tenant-test",
            connector_id="virtual_taobao",
            resource=resource,
            actor="admin-test",
        )
        assert result["virtual"] is True


def _configure_and_run(client: TestClient) -> dict:
    rebuilt = client.post(
        "/v1/forecasting/demand/rebuild",
        headers=ADMIN,
        json={
            "store_id": STORE,
            "sku_id": SKU,
            "mode": "full",
            "start_date": "2026-06-01",
            "end_date": "2026-07-21",
            "coverage_complete": True,
        },
    )
    assert rebuilt.status_code == 200, rebuilt.text
    assert rebuilt.json()["facts_written"] >= 51

    configured = client.put(
        f"/v1/forecasting/policies/{SKU}",
        headers=ADMIN,
        json=POLICY_UPDATE,
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["forecast_policy"]["write_status"] == "created"
    assert configured.json()["inventory_policy"]["write_status"] == "created"

    response = client.post(
        "/v1/forecasting/runs",
        headers=ADMIN,
        json={"store_id": STORE, "sku_id": SKU},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_forecasting_http_api_exposes_replayable_evidence_and_read_only_gets(
    tmp_path,
) -> None:
    app = create_app(make_settings(tmp_path))
    assert app.state.agent.settings.model_enabled is False
    expected_paths = {
        "/v1/forecasting/demand/rebuild",
        "/v1/forecasting/demand",
        "/v1/forecasting/runs",
        "/v1/forecasting/runs/{run_id}",
        "/v1/forecasting/skus/{sku_id}/forecast",
        "/v1/forecasting/skus/{sku_id}/backtest",
        "/v1/forecasting/policies/{sku_id}",
        "/v1/forecasting/skus/{sku_id}/inventory-plan",
        "/v1/forecasting/risks",
    }
    assert expected_paths <= set(app.openapi()["paths"])
    _prepare_virtual_inputs(app.state.agent)

    with TestClient(app) as client:
        result = _configure_and_run(client)
        forecast = result["forecast"]
        plan = result["inventory_plan"]
        assert result["inventory_plan_status"] == "created"
        assert forecast["forecast_policy_version"] == "forecast-wp4-v1"
        assert forecast["status"] == "degraded"
        assert len(forecast["points"]) == 30
        assert plan["forecast_run_id"] == forecast["run_id"]
        assert plan["planning_policy_version"] == "inventory-wp4-v1"
        assert plan["action_mode"] == "advisory_only"
        assert plan["plan_quality"] == "degraded"

        demand = client.get(
            "/v1/forecasting/demand",
            headers=ADMIN,
            params={"store_id": STORE, "sku_id": SKU},
        )
        assert demand.status_code == 200
        assert demand.json()["demand_policy"]["policy_version"] == "demand-v1"
        assert len(demand.json()["facts"]) >= 51
        assert demand.json()["quality_summary"]["fact_count"] == len(
            demand.json()["facts"]
        )

        run = client.get(
            f"/v1/forecasting/runs/{forecast['run_id']}", headers=ADMIN
        )
        latest = client.get(
            f"/v1/forecasting/skus/{SKU}/forecast",
            headers=ADMIN,
            params={"store_id": STORE},
        )
        backtest = client.get(
            f"/v1/forecasting/skus/{SKU}/backtest",
            headers=ADMIN,
            params={"store_id": STORE},
        )
        inventory_plan = client.get(
            f"/v1/forecasting/skus/{SKU}/inventory-plan",
            headers=ADMIN,
            params={"store_id": STORE},
        )
        risks = client.get(
            "/v1/forecasting/risks",
            headers=ADMIN,
            params={"store_id": STORE, "sku_id": SKU},
        )
        assert run.status_code == latest.status_code == backtest.status_code == 200
        assert inventory_plan.status_code == risks.status_code == 200
        assert run.json()["run_id"] == latest.json()["run_id"] == forecast["run_id"]
        assert backtest.json()["run_id"] == forecast["run_id"]
        assert backtest.json()["backtests"]
        assert inventory_plan.json()["plan_id"] == plan["plan_id"]
        assert risks.json()[0]["risk_evidence"]["selected_quantile"] == "p80"

        with app.state.agent.db.connect() as conn:
            audit_rows = conn.execute(
                """SELECT event_type, actor, tenant_id, detail_json
                FROM audit_log WHERE event_type LIKE 'forecasting.%'"""
            ).fetchall()
        audit = {row["event_type"]: row for row in audit_rows}
        assert {
            "forecasting.demand.rebuilt",
            "forecasting.policies.configured",
            "forecasting.run.completed",
        } <= audit.keys()
        assert all(row["actor"] == "admin-test" for row in audit.values())
        assert all(row["tenant_id"] == "tenant-test" for row in audit.values())
        run_audit = json.loads(audit["forecasting.run.completed"]["detail_json"])
        assert run_audit["forecast_status"] == forecast["status"]
        assert run_audit["inventory_plan_status"] == "created"

        counts_before = _evidence_counts(app.state.agent)
        for path in (
            f"/v1/forecasting/runs/{forecast['run_id']}",
            f"/v1/forecasting/skus/{SKU}/forecast?store_id={STORE}",
            f"/v1/forecasting/skus/{SKU}/backtest?store_id={STORE}",
            f"/v1/forecasting/skus/{SKU}/inventory-plan?store_id={STORE}",
            f"/v1/forecasting/risks?store_id={STORE}&sku_id={SKU}",
        ):
            assert client.get(path, headers=ADMIN).status_code == 200
        assert _evidence_counts(app.state.agent) == counts_before


def test_forecasting_api_hides_cross_tenant_evidence_and_maps_errors(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    app.state.agent.auth.create_admin_operator(
        "forecast-other-tenant",
        AdminOperatorCreateRequest(
            admin_id="forecast-other-admin",
            name="Forecast other tenant",
            key="forecast-other-key-123456",
        ),
        "admin-test",
    )
    _prepare_virtual_inputs(app.state.agent)
    with TestClient(app) as client:
        forecast = _configure_and_run(client)["forecast"]
        for path in (
            f"/v1/forecasting/skus/{SKU}/forecast",
            f"/v1/forecasting/skus/{SKU}/backtest",
            f"/v1/forecasting/skus/{SKU}/inventory-plan",
        ):
            assert client.get(path, headers=ADMIN).status_code == 422
        hidden_paths = (
            f"/v1/forecasting/runs/{forecast['run_id']}",
            f"/v1/forecasting/skus/{SKU}/forecast?store_id={STORE}",
            f"/v1/forecasting/skus/{SKU}/inventory-plan?store_id={STORE}",
        )
        assert all(
            client.get(path, headers=OTHER_ADMIN).status_code == 404
            for path in hidden_paths
        )
        risks = client.get(
            f"/v1/forecasting/risks?store_id={STORE}&sku_id={SKU}",
            headers=OTHER_ADMIN,
        )
        assert risks.status_code == 200
        assert risks.json() == []
        missing = client.get("/v1/forecasting/runs/not-owned", headers=ADMIN)
        assert missing.status_code == 404
        assert missing.json()["detail"] == "forecast_run_not_found"


def test_forecasting_policy_pair_is_atomic_and_rejects_same_version_drift(
    tmp_path,
) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        first = client.put(
            f"/v1/forecasting/policies/{SKU}", headers=ADMIN, json=POLICY_UPDATE
        )
        assert first.status_code == 200
        with app.state.agent.db.connect() as conn:
            before = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE tenant_id='tenant-test'"
                ).fetchone()[0]
                for table in ("forecast_policies", "inventory_planning_policies")
            }

        conflicting = deepcopy(POLICY_UPDATE)
        conflicting["forecast_policy"]["policy_version"] = "forecast-wp4-v2"
        conflicting["inventory_policy"]["supplier_lead_days"] = 9
        response = client.put(
            f"/v1/forecasting/policies/{SKU}", headers=ADMIN, json=conflicting
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "planning_policy_version_conflict"
        with app.state.agent.db.connect() as conn:
            after = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE tenant_id='tenant-test'"
                ).fetchone()[0]
                for table in ("forecast_policies", "inventory_planning_policies")
            }
            rolled_back = conn.execute(
                """SELECT 1 FROM forecast_policies
                WHERE tenant_id=? AND store_id=? AND sku_id=? AND policy_version=?""",
                ("tenant-test", STORE, SKU, "forecast-wp4-v2"),
            ).fetchone()
        assert after == before
        assert rolled_back is None


def test_forecasting_api_rejects_corrupt_persisted_policy_as_domain_error(
    tmp_path,
) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        configured = client.put(
            f"/v1/forecasting/policies/{SKU}", headers=ADMIN, json=POLICY_UPDATE
        )
        assert configured.status_code == 200
        with app.state.agent.db.connect() as conn:
            conn.execute(
                """UPDATE forecast_policies SET candidate_models_json=?
                WHERE tenant_id=? AND store_id=? AND sku_id=?""",
                ("{", "tenant-test", STORE, SKU),
            )

        response = client.post(
            "/v1/forecasting/runs",
            headers=ADMIN,
            json={"store_id": STORE, "sku_id": SKU},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "forecast_policy_evidence_invalid"


def test_forecasting_api_rejects_corrupt_persisted_run_as_domain_error(
    tmp_path,
) -> None:
    app = create_app(make_settings(tmp_path))
    _prepare_virtual_inputs(app.state.agent)
    with TestClient(app, raise_server_exceptions=False) as client:
        run = _configure_and_run(client)["forecast"]
        with app.state.agent.db.connect() as conn:
            conn.execute(
                """UPDATE forecast_runs SET candidate_models_json='{}'
                WHERE tenant_id=? AND run_id=?""",
                ("tenant-test", run["run_id"]),
            )

        for path in (
            f"/v1/forecasting/runs/{run['run_id']}",
            f"/v1/forecasting/skus/{SKU}/forecast?store_id={STORE}",
        ):
            response = client.get(path, headers=ADMIN)
            assert response.status_code == 409
            assert response.json()["detail"] == "forecast_run_evidence_invalid"


_FORECASTING_READ_ONLY_TABLES = (
    "demand_daily_facts",
    "inventory_balances",
    "forecast_policies",
    "forecast_runs",
    "forecast_backtests",
    "forecast_points",
    "forecast_anomalies",
    "inventory_planning_policies",
    "inventory_plans",
)


def _evidence_counts(service: AgentService) -> dict[str, int]:
    with service.db.connect() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in _FORECASTING_READ_ONLY_TABLES
        }


def _evidence_snapshot(service: AgentService) -> dict[str, list[tuple[object, ...]]]:
    with service.db.connect() as conn:
        return {
            table: [
                tuple(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in _FORECASTING_READ_ONLY_TABLES
        }


def _execute_read_tool_unchanged(
    service: AgentService,
    *,
    spec,
    arguments,
    context: ToolExecutionContext,
):
    before = _evidence_snapshot(service)
    result = service.tools.execute(spec=spec, arguments=arguments, context=context)
    assert _evidence_snapshot(service) == before
    return result


def test_forecasting_tools_read_only_persisted_evidence_via_dynamic_catalog(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    _prepare_virtual_inputs(app.state.agent)
    with TestClient(app) as client:
        run = _configure_and_run(client)

        service = app.state.agent
        modules = business_module_catalog()
        forecasting = next(item for item in modules if item.module_id == "forecasting")
        assert forecasting.status == "available"
        assert set(forecasting.agent_tools) == {
            "get_demand_forecast",
            "get_inventory_plan",
        }
        catalog = {item["name"]: item for item in service.tools.catalog_for_model()}
        assert set(forecasting.agent_tools) <= catalog.keys()
        assert all(catalog[name]["kind"] == "read" for name in forecasting.agent_tools)

        context = ToolExecutionContext(
            tenant_id="tenant-test",
            client_id="client-test",
            session_id="session-test",
            trace_id="trace-test",
            trusted_context={"store_id": STORE},
        )
        outputs = {}
        for name in forecasting.agent_tools:
            spec, arguments = service.tools.validate_selection(
                name=name,
                arguments={"store_id": STORE, "sku_id": SKU},
                requested_mode="observe",
                context=context,
            )
            outputs[name] = _execute_read_tool_unchanged(
                service, spec=spec, arguments=arguments, context=context
            ).output

        forecast = outputs["get_demand_forecast"]
        assert forecast["evidence_source"] == "forecast_runs"
        assert forecast["computed_now"] is False
        assert forecast["forecast"]["run_id"] == run["forecast"]["run_id"]
        assert forecast["references"]["data_quality"] == "degraded"
        plan = outputs["get_inventory_plan"]
        assert plan["evidence_source"] == "inventory_plans"
        assert plan["computed_now"] is False
        assert plan["action_allowed"] is False
        assert plan["inventory_plan"]["plan_id"] == run["inventory_plan"]["plan_id"]
        assert plan["references"]["inventory_snapshot_hash"]
        assert plan["references"]["planning_policy_version"] == "inventory-wp4-v1"
        assert plan["references"]["quality_issues"] == (
            plan["inventory_plan"]["quality_issues"]
        )

        unscoped = ToolExecutionContext(
            tenant_id="tenant-test",
            client_id="client-test",
            session_id="session-unscoped",
            trace_id="trace-unscoped",
            trusted_context={},
        )
        with pytest.raises(
            ValueError, match="tool_policy_denied:store_scope_required"
        ):
            service.tools.validate_selection(
                name="get_demand_forecast",
                arguments={"sku_id": SKU},
                requested_mode="observe",
                context=unscoped,
            )


@pytest.mark.parametrize(
    "statement, params",
    (
        (
            """UPDATE forecast_runs SET champion_model='mutated-model'
            WHERE tenant_id=? AND store_id=? AND sku_id=?""",
            (STORE, SKU),
        ),
        (
            """UPDATE forecast_policies SET minimum_history_days=999
            WHERE tenant_id=? AND store_id=? AND sku_id=?""",
            (STORE, SKU),
        ),
        (
            """UPDATE inventory_balances SET on_hand='999'
            WHERE tenant_id=? AND store_id=? AND sku_id=?""",
            (STORE, SKU),
        ),
    ),
)
def test_read_only_tool_snapshot_gate_detects_in_place_update(
    tmp_path,
    statement: str,
    params: tuple[str, str],
) -> None:
    app = create_app(make_settings(tmp_path))
    _prepare_virtual_inputs(app.state.agent)
    with TestClient(app) as client:
        _configure_and_run(client)

        service = app.state.agent
        context = ToolExecutionContext(
            tenant_id="tenant-test",
            client_id="client-test",
            session_id="session-mutation",
            trace_id="trace-mutation",
            trusted_context={"store_id": STORE},
        )
        spec, arguments = service.tools.validate_selection(
            name="get_demand_forecast",
            arguments={"store_id": STORE, "sku_id": SKU},
            requested_mode="observe",
            context=context,
        )
        original_handler = spec.handler

        def mutating_handler(arguments, context):
            with service.db.connect() as conn:
                conn.execute(statement, (context.tenant_id, *params))
            return original_handler(arguments, context)

        with pytest.raises(AssertionError):
            _execute_read_tool_unchanged(
                service,
                spec=replace(spec, handler=mutating_handler),
                arguments=arguments,
                context=context,
            )


class _ForecastConsoleStructure(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.nav_views: set[str] = set()
        self.fields: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "button" and values.get("data-view"):
            self.nav_views.add(values["data-view"])
        if values.get("data-forecast-field"):
            self.fields.add(values["data-forecast-field"])


def test_forecasting_console_has_complete_structured_evidence_without_auto_run(
    tmp_path,
) -> None:
    app = create_app(make_settings(tmp_path))
    before = _evidence_counts(app.state.agent)
    with TestClient(app) as client:
        page = client.get("/admin")
    structure = _ForecastConsoleStructure()
    structure.feed(page.text)
    assert page.status_code == 200
    assert _evidence_counts(app.state.agent) == before
    assert "forecasting" in structure.nav_views
    assert {
        "forecastStore",
        "forecastSku",
        "loadForecastEvidence",
        "runForecast",
        "forecastDemandRows",
        "forecastPointRows",
        "forecastBacktestRows",
        "forecastRiskDetail",
    } <= structure.ids
    assert {
        "historical_demand",
        "forecast_interval",
        "inventory_line",
        "stockout_date",
        "recommended_quantity",
        "backtest",
        "data_quality",
    } <= structure.fields
