from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.business.orders import (
    LogisticsSnapshotInput,
    OrderLineInput,
    OrderUpsert,
)
from ecommerce_agent.schemas import (
    HandoffOperatorQueueAssignment,
    HandoffOperatorUpsert,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


SOURCE_TIME = datetime(2026, 7, 27, 6, 0, tzinfo=UTC)


def _order() -> OrderUpsert:
    return OrderUpsert(
        connector_id="test-connector",
        store_id="store-001",
        order_id="ORDER-1001",
        order_status="shipped",
        payment_status="paid",
        currency="CNY",
        total_amount="499.00",
        placed_at=SOURCE_TIME - timedelta(days=1),
        buyer_ref_hash="a" * 32,
        lines=[
            OrderLineInput(
                line_id="line-001",
                sku_id="sku-001",
                title="测试空气炸锅",
                quantity=1,
                unit_price="499.00",
            )
        ],
        logistics=LogisticsSnapshotInput(
            carrier="测试快递",
            tracking_no_masked="TEST****1001",
            status="in_transit",
            last_event="运输中",
            last_event_at=SOURCE_TIME,
        ),
        after_sales=[],
        source_updated_at=SOURCE_TIME,
        source_id="source-order-1001",
    )


def _make_bootstrap_operator_available(service: AgentService) -> str:
    tenant_id = service.settings.bootstrap_tenant_id
    operator_id = service.settings.bootstrap_admin_id
    current = service.handoff_staffing.get(
        tenant_id=tenant_id,
        operator_id=operator_id,
    )
    assert current is not None
    queues = service.handoffs.list_queues(tenant_id=tenant_id)
    service.handoff_staffing.upsert(
        tenant_id=tenant_id,
        operator_id=operator_id,
        value=HandoffOperatorUpsert(
            display_name=current.display_name,
            presence="available",
            dispatch_mode="manual",
            schedule_mode="unrestricted",
            max_active_tasks=current.max_active_tasks,
            skills=current.skills,
            queue_assignments=[
                HandoffOperatorQueueAssignment(
                    queue_key=queue.queue_key,
                    skill_level=3,
                    is_primary=queue.queue_key == "general",
                )
                for queue in queues
                if queue.status == "active"
            ],
            expected_record_version=current.record_version,
        ),
        actor=operator_id,
    )
    return operator_id


def test_started_handoff_marks_linked_order_processing_without_creating_after_sale(
    tmp_path,
) -> None:
    app = create_app(make_settings(tmp_path))
    headers = {
        "X-Admin-Id": "admin-test",
        "X-Admin-Key": "test-admin-key-123456",
    }
    with TestClient(app) as client:
        service = app.state.agent
        tenant_id = service.settings.bootstrap_tenant_id
        service.operations.orders.upsert(tenant_id, _order())
        service.model.generate_json = lambda _messages, **_kwargs: {  # type: ignore[method-assign]
            "intent": "refund",
            "mode": "act",
            "tool_name": "refund_order",
            "arguments": {},
            "expected_outcome": "人工核对退款条件",
            "reason": "customer_requested_refund",
            "confidence": 0.95,
        }

        response = service.chat(
            principal_for(service),
            "order-handoff-visibility",
            "订单号 ORDER-1001，请马上帮我退款。",
            {"shop_id": "store-001"},
            source_type="simulation",
            source_reference="local-customer-test",
        )
        assert response.requires_human is True
        assert response.handoff_id is not None
        task = service.handoffs.get(
            tenant_id=tenant_id,
            handoff_id=response.handoff_id,
        )
        assert task.payload["business_context"] == {
            "order_id": "ORDER-1001",
            "store_id": "store-001",
        }

        operator_id = _make_bootstrap_operator_available(service)
        claimed = service.handoffs.claim(
            tenant_id=tenant_id,
            handoff_id=task.id,
            operator=operator_id,
            expected_version=task.version,
            note="认领订单售后咨询",
        )
        service.handoffs.transition(
            tenant_id=tenant_id,
            handoff_id=task.id,
            target_status="working",
            operator=operator_id,
            expected_version=claimed.version,
            note="开始核对订单信息",
        )

        operational_response = client.get(
            "/v1/orders?order_id=ORDER-1001&scope=operational",
            headers=headers,
        )
        simulation_response = client.get(
            "/v1/orders?order_id=ORDER-1001&scope=simulation",
            headers=headers,
        )
        assert operational_response.status_code == 200
        assert simulation_response.status_code == 200
        operational_order = operational_response.json()[0]
        simulation_order = simulation_response.json()[0]

        assert operational_order["customer_service"] is None
        assert simulation_order["customer_service"]["status"] == "processing"
        assert simulation_order["customer_service"]["label"] == "客服处理中"
        assert simulation_order["customer_service"]["task_status"] == "working"
        assert simulation_order["order_status"] == "shipped"
        assert simulation_order["payment_status"] == "paid"
        assert simulation_order["after_sales"] == []
        with service.db.connect() as conn:
            case_count = conn.execute(
                "SELECT COUNT(*) FROM commerce_after_sale_cases"
            ).fetchone()[0]
        assert case_count == 0


def test_orders_console_distinguishes_customer_service_from_after_sale() -> None:
    page = (
        Path(__file__).resolve().parents[1] / "docs" / "admin-console.html"
    ).read_text(encoding="utf-8")

    assert 'id="orderServiceScope"' in page
    assert "客服状态仅表示人工任务流转，不创建退款或售后申请" in page
    assert "item.customer_service" in page
    assert "客服处理中" in page
