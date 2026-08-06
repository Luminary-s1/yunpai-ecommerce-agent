from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ecommerce_agent.api import create_app
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


ADMIN_HEADERS = {
    "X-Admin-Id": "admin-test",
    "X-Admin-Key": "test-admin-key-123456",
}
CLIENT_HEADERS = {
    "X-Client-Id": "client-test",
    "X-Client-Key": "test-client-key-12345",
    "X-Subject-Id": "buyer-context",
}


def test_agent_persists_generation_snapshot_and_message_link(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        response = service.chat(
            principal_for(service),
            "context-answer",
            "尺码怎么选",
            {"platform": "admin-preview", "store_id": "store-a", "sku_id": "sku-a"},
        )

        assert response.context_snapshot_id
        assert response.context_readiness == "ready"
        assert response.evidence_ids
        snapshot = service.contexts.get("tenant-test", response.context_snapshot_id)
        assert snapshot is not None
        assert snapshot.stage == "generation"
        assert snapshot.parent_snapshot_id
        assert any(item["type"] == "knowledge" for item in snapshot.evidence)
        assert snapshot.bundle["trusted_session_state"]["store_id"] == "store-a"
        with service.db.connect() as conn:
            row = conn.execute(
                "SELECT context_snapshot_id FROM messages WHERE id=?",
                (response.message_id,),
            ).fetchone()
            count = conn.execute(
                "SELECT COUNT(*) FROM context_snapshots WHERE trace_id=?",
                (response.trace_id,),
            ).fetchone()[0]
        assert row["context_snapshot_id"] == response.context_snapshot_id
        assert count == 2
    finally:
        service.close()


def test_pronoun_turn_reuses_previous_context_builder_subject_for_retrieval(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        principal = principal_for(service)
        service.chat(
            principal,
            "context-pronoun",
            "我在看晴川空气炸锅 5L 云白款",
            {"shop_id": "qingchuan-flagship-001", "sku_id": "QC-AF5-WHITE"},
        )
        response = service.chat(
            principal,
            "context-pronoun",
            "它多少钱？",
            {"shop_id": "qingchuan-flagship-001"},
        )

        snapshot = service.contexts.get("tenant-test", response.context_snapshot_id or "")
        assert snapshot is not None
        assert snapshot.bundle["current_subject"]["sku_id"] == "QC-AF5-WHITE"
    finally:
        service.close()


def test_latest_subject_ignores_a_tampered_snapshot(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        principal = principal_for(service)
        response = service.chat(
            principal,
            "context-tamper",
            "我在看晴川空气炸锅 5L 云白款",
            {"shop_id": "qingchuan-flagship-001", "sku_id": "QC-AF5-WHITE"},
        )
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                "UPDATE context_snapshots SET bundle_json=? WHERE id=?",
                ('{"current_subject":{"sku_id":"QC-UNTRUSTED"}}', response.context_snapshot_id),
            )

        assert service.contexts.latest_subject("tenant-test", "context-tamper") == {}
    finally:
        service.close()


def test_conflicting_store_identity_handoffs_before_model_execution(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    model_called = False

    def fail_if_called(_messages):
        nonlocal model_called
        model_called = True
        raise AssertionError("model must not receive conflicting identity context")

    service.model.generate_json = fail_if_called  # type: ignore[method-assign]
    try:
        response = service.chat(
            principal_for(service),
            "context-conflict",
            "尺码怎么选",
            {"store_id": "store-a", "shop_id": "store-b"},
        )
        assert response.requires_human is True
        assert response.reason == "context_evidence_conflict"
        assert response.context_readiness == "handoff_required"
        assert model_called is False
        snapshot = service.contexts.get("tenant-test", response.context_snapshot_id or "")
        assert snapshot is not None
        assert snapshot.conflicts[0]["code"] == "store_id_identity_conflict"
    finally:
        service.close()


def test_context_snapshot_is_idempotent_concurrent_and_tenant_scoped(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        session_id = service.db.resolve_session(
            tenant_id="tenant-test",
            client_id="client-test",
            external_session_id="context-concurrent",
            subject_hash="subject-hash",
        )

        def build():
            return service.contexts.build(
                tenant_id="tenant-test",
                session_id=session_id,
                trace_id="trace-concurrent",
                stage="decision",
                sequence=0,
                question="查询商品",
                trusted_context={"platform": "test", "shop_policy": "联系 13800138000"},
                documents=[],
                sops=[],
                tool_catalog=[],
                history=[],
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            snapshots = list(pool.map(lambda _: build(), range(16)))
        assert len({item.id for item in snapshots}) == 1
        assert len({item.checksum for item in snapshots}) == 1
        assert "13800138000" not in str(snapshots[0].bundle)
        assert service.contexts.get("other-tenant", snapshots[0].id) is None
        with service.db.connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM context_snapshots WHERE trace_id='trace-concurrent'"
            ).fetchone()[0]
        assert count == 1
    finally:
        service.close()


def test_context_snapshot_detects_replay_change_and_persisted_tampering(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        session_id = service.db.resolve_session(
            tenant_id="tenant-test",
            client_id="client-test",
            external_session_id="context-integrity",
            subject_hash="subject-hash",
        )
        arguments = dict(
            tenant_id="tenant-test",
            session_id=session_id,
            trace_id="trace-integrity",
            stage="decision",
            sequence=0,
            question="查询商品",
            trusted_context={"store_id": "store-a"},
            documents=[],
            sops=[],
            tool_catalog=[],
            history=[],
        )
        snapshot = service.contexts.build(**arguments)
        with pytest.raises(RuntimeError, match="replay mismatch"):
            service.contexts.build(**{**arguments, "trusted_context": {"store_id": "store-b"}})
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                "UPDATE context_snapshots SET readiness='handoff_required' WHERE id=?",
                (snapshot.id,),
            )
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            service.contexts.get("tenant-test", snapshot.id)
    finally:
        service.close()


def test_admin_can_inspect_only_its_tenant_context_snapshot(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as client:
        chat = client.post(
            "/v1/chat",
            headers=CLIENT_HEADERS,
            json={"session_id": "context-admin", "message": "尺码怎么选", "context": {}},
        )
        assert chat.status_code == 200
        snapshot_id = chat.json()["context_snapshot_id"]
        detail = client.get(f"/v1/admin/context-snapshots/{snapshot_id}", headers=ADMIN_HEADERS)
        assert detail.status_code == 200
        assert detail.json()["id"] == snapshot_id
        assert detail.json()["evidence"]
        assert client.get("/v1/admin/context-snapshots/ctx-missing", headers=ADMIN_HEADERS).status_code == 404
        assert client.get(f"/v1/admin/context-snapshots/{snapshot_id}").status_code == 401


def test_authorized_order_context_is_recorded_as_platform_evidence(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), bootstrap_client_can_supply_order_context=True)
    service = AgentService(settings)
    try:
        response = service.chat(
            principal_for(service),
            "context-order",
            "我的订单状态是什么",
            {"order_id": "order-1001", "order_status": "paid"},
        )
        snapshot = service.contexts.get("tenant-test", response.context_snapshot_id or "")
        assert snapshot is not None
        business = next(item for item in snapshot.evidence if item["type"] == "business_context")
        assert business["authority"] == "authorized_platform_context"
        assert snapshot.bundle["current_subject"]["order_id"] == "order-1001"
    finally:
        service.close()
