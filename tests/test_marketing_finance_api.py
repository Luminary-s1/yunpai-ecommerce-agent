from __future__ import annotations

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}
STORE_ID = "qingchuan-flagship-001"


def _seed_virtual_store(client: TestClient) -> None:
    response = client.post(
        "/v1/simulations/virtual-store/run",
        headers=ADMIN_HEADERS,
        json={
            "fixture_id": "qingchuan-home-appliance-v1",
            "confirm_virtual": True,
            "include_customer_service": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["passed"] is True


def test_marketing_and_finance_api_return_operational_outputs(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        _seed_virtual_store(client)

        performance = client.get(
            f"/v1/marketing/performance?store_id={STORE_ID}", headers=ADMIN_HEADERS
        )
        assert performance.status_code == 200
        assert len(performance.json()) == 2

        diagnosis = client.post(
            "/v1/marketing/diagnosis",
            headers=ADMIN_HEADERS,
            json={"store_id": STORE_ID, "min_roas": "2.00"},
        )
        assert diagnosis.status_code == 200
        assert any(
            item["code"] == "high_spend_no_orders"
            for item in diagnosis.json()["findings"]
        )

        draft = client.post(
            "/v1/marketing/content-drafts",
            headers=ADMIN_HEADERS,
            json={
                "draft_key": "api-copy-check",
                "store_id": STORE_ID,
                "content_type": "product_copy",
                "title": "Draft for fact checking",
                "body": "Claims remain subject to human review.",
                "sku_ids": ["QC-AF5-WHITE"],
                "declared_prices": {"QC-AF5-WHITE": "499.00"},
                "source_type": "manual",
                "expected_record_version": 0,
            },
        )
        assert draft.status_code == 200
        assert draft.json()["fact_check"]["passed"] is True
        assert draft.json()["publication_allowed"] is False

        profit = client.post(
            "/v1/finance/profit",
            headers=ADMIN_HEADERS,
            json={"store_id": STORE_ID, "start_date": "2026-07-10", "end_date": "2026-07-22"},
        )
        assert profit.status_code == 200
        assert profit.json()["management_profit"] == "1491.00"
        assert profit.json()["data_quality"]["financial_statement"] is False

        tasks = client.get(
            f"/v1/finance/reconciliation/tasks?store_id={STORE_ID}",
            headers=ADMIN_HEADERS,
        )
        assert tasks.status_code == 200
        task = next(item for item in tasks.json() if item["difference_amount"] == "-16.00")
        transition = client.post(
            f"/v1/finance/reconciliation/tasks/{task['id']}/transition",
            headers=ADMIN_HEADERS,
            json={
                "target_status": "reviewing",
                "expected_record_version": task["record_version"],
                "note": "Manual variance review started.",
            },
        )
        assert transition.status_code == 200
        assert transition.json()["status"] == "reviewing"
