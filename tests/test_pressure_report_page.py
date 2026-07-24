from __future__ import annotations

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


def test_marketing_finance_pressure_report_serves_full_evidence(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        page = client.get("/reports/marketing-finance-pressure")
        assert page.status_code == 200
        assert "营销与利润模块压测报告" in page.text
        assert "/reports/marketing-finance-pressure.json" in page.text

        evidence = client.get("/reports/marketing-finance-pressure.json")
        assert evidence.status_code == 200
        payload = evidence.json()
        assert payload["contract"] == "marketing-finance-pressure-v1"
        assert payload["completed_operations"] == 818
        assert payload["inputs"]["marketing_metric"]["campaign_id"] == "campaign-pressure"
        assert payload["write_samples"]["marketing"]["operation_count"] == 128
        assert len(payload["content_draft_outputs"]) == 64
        assert len(payload["reconciliation_task"]["run_outputs"]) == 64
        assert payload["tenant_isolation_sample"]["tasks"] == []
