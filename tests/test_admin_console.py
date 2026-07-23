from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app

from conftest import make_settings


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}
CLIENT_HEADERS = {
    "X-Client-Id": "client-test",
    "X-Client-Key": "test-client-key-12345",
    "X-Subject-Id": "buyer-admin-console",
}


def test_admin_console_aggregates_customer_service_data(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={
                "session_id": "admin-console-session",
                "message": "退款多久到账？",
                "context": {},
            },
        )
        assert response.status_code == 200

        overview = client.get("/v1/admin/overview", headers=ADMIN_HEADERS)
        assert overview.status_code == 200
        assert overview.json()["counts"]["conversations"] == 1
        assert overview.json()["counts"]["messages"] == 2
        assert overview.json()["metrics"]["requests"] == 1

        conversations = client.get(
            "/v1/admin/conversations?query=admin-console", headers=ADMIN_HEADERS
        )
        assert conversations.status_code == 200
        item = conversations.json()["items"][0]
        assert item["external_session_id"] == "admin-console-session"
        assert item["message_count"] == 2

        detail = client.get(
            f"/v1/admin/conversations/{item['id']}", headers=ADMIN_HEADERS
        )
        assert detail.status_code == 200
        assert [message["role"] for message in detail.json()["messages"]] == [
            "user",
            "assistant",
        ]
        assert "sources_json" not in detail.json()["messages"][1]
        context = detail.json()["messages"][1]["context"]
        assert context["stage"] == "generation"
        assert context["readiness"] == "ready"

        assert client.get("/v1/admin/overview").status_code == 401
        assert client.get("/v1/admin/conversations/session-missing", headers=ADMIN_HEADERS).status_code == 404


def test_admin_console_isolates_simulation_sessions_and_exposes_decisions(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        live_response = client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={
                "session_id": "live-admin-scope",
                "message": "退款多久到账？",
                "context": {},
            },
        )
        assert live_response.status_code == 200

        service = app.state.agent
        principal = service.auth.authenticate(
            "client-test",
            "test-client-key-12345",
            "buyer-admin-console-simulation",
        )
        simulated = service.chat(
            principal,
            "virtual-admin-scope",
            "我要投诉，立即转人工",
            {},
            source_type="simulation",
            source_reference="simulation-run-test",
        )
        assert simulated.requires_human is True

        operational = client.get("/v1/admin/overview", headers=ADMIN_HEADERS).json()
        simulation = client.get(
            "/v1/admin/overview?scope=simulation", headers=ADMIN_HEADERS
        ).json()
        all_data = client.get(
            "/v1/admin/overview?scope=all", headers=ADMIN_HEADERS
        ).json()
        assert operational["data_scope"] == "operational"
        assert operational["counts"]["conversations"] == 1
        assert operational["excluded_sessions"] == {"simulation": 1}
        assert simulation["counts"]["conversations"] == 1
        assert all_data["counts"]["conversations"] == 2

        live_items = client.get(
            "/v1/admin/conversations", headers=ADMIN_HEADERS
        ).json()["items"]
        simulation_items = client.get(
            "/v1/admin/conversations?scope=simulation", headers=ADMIN_HEADERS
        ).json()["items"]
        assert [item["source_type"] for item in live_items] == ["api"]
        assert [item["source_type"] for item in simulation_items] == ["simulation"]
        assert simulation_items[0]["source_reference"] == "simulation-run-test"

        detail = client.get(
            f"/v1/admin/conversations/{simulation_items[0]['id']}",
            headers=ADMIN_HEADERS,
        ).json()
        assistant_message = detail["messages"][-1]
        assert detail["session"]["source_type"] == "simulation"
        assert assistant_message["model_fallback"] is False
        assert assistant_message["route_reason"]
        assert assistant_message["decision"]["decision_mode"]
        assert assistant_message["decision"]["trace"]

        assert client.get("/v1/handoffs", headers=ADMIN_HEADERS).json() == []
        simulation_handoffs = client.get(
            "/v1/handoffs?scope=simulation", headers=ADMIN_HEADERS
        ).json()
        assert len(simulation_handoffs) == 1
        assert simulation_handoffs[0]["source_type"] == "simulation"
        assert simulation_handoffs[0]["source_reference"] == "simulation-run-test"


def test_admin_console_page_and_audit_api(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        page = client.get("/admin")
        assert page.status_code == 200
        assert "yunpai-admin-console" in page.text
        assert "竞品分析" in page.text
        assert "商品与库存" in page.text
        assert "订单、物流与售后" in page.text
        assert "active_sku_count" in page.text
        assert "知识与 SOP" in page.text
        assert "质检与 VOC" in page.text
        assert "客服 Agent 评测" in page.text
        assert 'id="evaluationSuiteForm"' in page.text
        assert 'id="evaluationCases"' in page.text
        assert 'id="runEvaluationSuite"' in page.text
        assert "/v1/admin/evaluations/overview" in page.text
        assert "/v1/admin/evaluations/suites" in page.text
        assert "renderEvaluationRun" in page.text
        assert "虚拟店铺场景验收" in page.text
        assert 'id="simulationScenarioRows"' in page.text
        assert 'id="simulationDetail"' in page.text
        assert 'id="runVirtualSimulation"' in page.text
        assert "/v1/simulations/virtual-store/run" in page.text
        assert "renderSimulationDetail" in page.text
        assert "真实调用输入" in page.text
        assert "实际业务输出" in page.text
        assert "渠道接待工作台" in page.text
        assert "reply-drafts" in page.text
        assert "发送队列与人工核对" in page.text
        assert "/v1/integrations/taobao/outbox/summary" in page.text
        assert "渠道 Agent 运行账本" in page.text
        assert "/v1/integrations/taobao/agent-jobs/summary" in page.text
        assert 'id="runAgentJobs"' in page.text
        assert "not_delivered" in page.text
        assert "SOP 运行与人工恢复" in page.text
        assert "/v1/admin/sop-runs?limit=100" in page.text
        assert "data-sop-resolution" in page.text
        assert "compensate" in page.text
        assert "DSL v2" in page.text
        assert 'id="sopActionDialog"' in page.text
        assert 'id="sopActionForm"' in page.text
        assert "openSopActionDialog" in page.text
        assert "处置说明至少需要 2 个字符" in page.text
        assert "持久告警队列" in page.text
        assert 'id="competitiveMonitorForm"' in page.text
        assert 'id="competitiveAlertDialog"' in page.text
        assert "/v1/competitive/monitors/evaluate-all" in page.text
        assert "openCompetitiveAlertDialog" in page.text
        assert 'id="contextEvidenceDialog"' in page.text
        assert "/v1/admin/context-snapshots/" in page.text
        assert "data-context-snapshot" in page.text
        assert 'id="handoffKpis"' in page.text
        assert 'id="handoffActionDialog"' in page.text
        assert 'id="dispatchActionDialog"' in page.text
        assert 'id="dispatchActionForm"' in page.text
        assert 'id="handoffQueueDialog"' in page.text
        assert 'id="handoffOperatorDialog"' in page.text
        assert 'id="handoffShiftManager"' in page.text
        assert 'id="handoffHistoryDialog"' in page.text
        assert 'id="handoffDispatchRows"' in page.text
        assert 'id="handoffDispatchAlerts"' in page.text
        assert "/v1/handoffs/summary" in page.text
        assert "/v1/handoffs/escalate-due" in page.text
        assert "/v1/handoffs/dispatch/summary" in page.text
        assert "/v1/handoffs/dispatch/alerts/" in page.text
        assert "/presence-sessions" in page.text
        assert "/shifts" in page.text
        assert "openHandoffActionDialog" in page.text
        assert "openDispatchActionDialog" in page.text
        assert "openHandoffQueueDialog" in page.text
        assert "window.prompt('填写告警确认说明" not in page.text
        assert "window.prompt('填写重试原因" not in page.text
        assert "adminAuthRequired" in page.text
        assert "local_principal_id" in page.text
        assert 'id="serviceScope"' in page.text
        assert 'id="chatMessage"' in page.text
        assert 'id="newCustomerTestSession"' in page.text
        assert "/v1/test/customer-chat" in page.text
        assert "localTestApi" in page.text
        assert "customerTestSessionId" in page.text
        assert "qingchuan-flagship-001" in page.text
        assert 'id="clientKey"' not in page.text
        assert "X-Client-Key" not in page.text
        assert "Mock 模拟模型" in page.text
        assert "决策详情" in page.text
        assert "max-width: 1440px" in page.text
        assert 'class="overview-columns"' in page.text
        assert "#recentActivity { max-height: 330px; overflow: auto; }" in page.text
        assert "min-height: clamp(360px, 44vh, 460px); max-height: 520px" in page.text
        assert "#chatResult { max-height: 280px; overflow: auto; }" in page.text

        client.post(
            "/v1/connectors/virtual_taobao/sync",
            headers=ADMIN_HEADERS,
            json={"resource": "competitor_price"},
        )
        audit = client.get(
            "/v1/admin/audit?event_type=connector.sync.succeeded",
            headers=ADMIN_HEADERS,
        )
        assert audit.status_code == 200
        assert audit.json()[0]["event_type"] == "connector.sync.succeeded"
        assert audit.json()[0]["detail"]["virtual"] is True


def test_local_admin_bypass_is_loopback_only_and_keeps_client_authentication(tmp_path) -> None:
    settings = replace(make_settings(tmp_path / "local"), admin_auth_required=False)
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["administrator"] == {
            "configured": True,
            "authentication_required": False,
            "local_bypass": True,
            "local_principal_id": "admin-test",
        }
        assert client.get("/v1/admin/overview").status_code == 200
        assert client.post(
            "/v1/chat",
            json={"session_id": "still-protected", "message": "你好", "context": {}},
        ).status_code == 401

    remote_app = create_app(
        replace(make_settings(tmp_path / "remote"), admin_auth_required=False)
    )
    with TestClient(remote_app, client=("192.0.2.10", 50000)) as client:
        response = client.get("/v1/admin/overview")
        assert response.status_code == 403
        assert "loopback" in response.json()["detail"]
