from __future__ import annotations

from pathlib import Path

from ecommerce_agent.config import Settings
from ecommerce_agent.auth import Principal


def make_settings(data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        model_provider="glm",
        model_base_url="http://127.0.0.1:9/v1",
        model_name="test-14b",
        model_api_key="",
        model_timeout_seconds=0.2,
        model_max_output_tokens=200,
        model_temperature=0.0,
        model_thinking_enabled=False,
        model_streaming=False,
        model_retry_attempts=0,
        model_enabled=False,
        model_mock_mode=True,
        model_context_limit_tokens=128000,
        context_budget_ratio=0.7,
        rag_top_k=5,
        rag_min_score=0.08,
        rag_direct_approved_answer=True,
        rag_direct_approved_min_score=0.6,
        max_input_chars=2000,
        session_history_limit=6,
        admin_api_key="test-admin-key-123456",
        admin_auth_required=True,
        bootstrap_admin_id="admin-test",
        auth_required=True,
        bootstrap_tenant_id="tenant-test",
        bootstrap_client_id="client-test",
        bootstrap_client_key="test-client-key-12345",
        bootstrap_client_can_supply_order_context=False,
        subject_hash_key="test-subject-hash-key-12345",
        message_retention_days=30,
        audit_retention_days=180,
        max_request_body_bytes=2048,
        rate_limit_requests_per_minute=100,
        min_free_disk_mb=1,
    )


def principal_for(service, subject_id: str = "buyer-1") -> Principal:
    return service.auth.authenticate(
        service.settings.bootstrap_client_id,
        service.settings.bootstrap_client_key,
        subject_id,
    )
