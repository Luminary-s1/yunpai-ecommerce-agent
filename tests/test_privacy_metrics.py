from __future__ import annotations

from dataclasses import replace

from ecommerce_agent.service import AgentService
from ecommerce_agent.database import Database
from ecommerce_agent.evolution import EvolutionService
from ecommerce_agent.schemas import FeedbackRequest

from conftest import make_settings, principal_for


def test_metric_p95_uses_nearest_rank(tmp_path) -> None:
    db = Database(tmp_path / "metrics.sqlite3")
    db.initialize()
    for index, duration in enumerate((10.0, 100.0), start=1):
        db.record_metric(
            trace_id=f"trace-{index}",
            tenant_id="tenant",
            session_id="session",
            intent="test",
            route_reason="test",
            success=True,
            model_fallback=False,
            requires_human=False,
            duration_ms=duration,
        )
    assert db.metric_summary("tenant")["latency_ms_p95"] == 100.0


def test_sensitive_content_is_redacted_before_persistence(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        service.chat(principal_for(service), "privacy-session", "我的手机号是13800138000，帮我改地址")
        with service.db.connect() as conn:
            row = conn.execute(
                "SELECT content, redacted FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        assert "13800138000" not in row["content"]
        assert "138****8000" in row["content"]
        assert row["redacted"] == 1
    finally:
        service.close()


def test_metrics_and_retention_dry_run_then_apply(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), message_retention_days=1, audit_retention_days=1)
    service = AgentService(settings)
    try:
        service.chat(principal_for(service), "old-session", "尺码怎么选")
        summary = service.db.metric_summary("tenant-test")
        assert summary["requests"] == 1
        assert summary["success_rate"] == 1.0

        old = "2000-01-01T00:00:00+00:00"
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute("UPDATE messages SET created_at=?", (old,))
            conn.execute("UPDATE context_snapshots SET created_at=?", (old,))
            conn.execute("UPDATE request_metrics SET created_at=?", (old,))
            conn.execute("UPDATE audit_log SET created_at=?", (old,))
            conn.execute("UPDATE sessions SET last_seen_at=?", (old,))

        preview = service.purge_expired(actor="test", dry_run=True)
        assert preview["messages_deleted"] == 2
        assert preview["context_snapshots_deleted"] == 2
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2

        applied = service.purge_expired(actor="test", dry_run=False)
        assert applied["messages_deleted"] == 2
        assert applied["checkpoints_deleted"] == 1
        with service.db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0] == 0
            assert conn.execute("SELECT status FROM sessions").fetchone()[0] == "closed"
    finally:
        service.close()


def test_retention_preserves_nonterminal_handoff_context(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), message_retention_days=1)
    service = AgentService(settings)
    try:
        response = service.chat(principal_for(service), "active-handoff", "帮我退款")
        old = "2000-01-01T00:00:00+00:00"
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute("UPDATE messages SET created_at=?", (old,))
            conn.execute("UPDATE context_snapshots SET created_at=?", (old,))
            conn.execute("UPDATE sessions SET last_seen_at=?", (old,))
            conn.execute("UPDATE handoff_tasks SET updated_at=?", (old,))
        report = service.purge_expired(actor="test", dry_run=False)
        with service.db.connect() as conn:
            session_status = conn.execute("SELECT status FROM sessions").fetchone()[0]
            message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            snapshot_count = conn.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0]
            handoff_status = conn.execute(
                "SELECT status FROM handoff_tasks WHERE id=?", (response.handoff_id,)
            ).fetchone()[0]
        assert session_status == "active"
        assert message_count == 2
        assert snapshot_count == 1
        assert report["context_snapshots_deleted"] == 0
        assert handoff_status == "proposed"
        assert report["checkpoints_deleted"] == 0
    finally:
        service.close()


def test_retention_removes_snapshot_but_keeps_redacted_feedback_message(tmp_path) -> None:
    settings = replace(make_settings(tmp_path), message_retention_days=1)
    service = AgentService(settings)
    evolution = EvolutionService(service.db, service.knowledge)
    try:
        response = service.chat(principal_for(service), "feedback-retention", "尺码怎么选")
        evolution.submit_feedback(
            FeedbackRequest(
                message_id=response.message_id,
                rating=-1,
                corrected_answer="请联系人工核对尺码。",
                evidence_source="人工复核记录",
            ),
            tenant_id="tenant-test",
        )
        old = "2000-01-01T00:00:00+00:00"
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute("UPDATE messages SET created_at=?", (old,))
            conn.execute("UPDATE context_snapshots SET created_at=?", (old,))
        report = service.purge_expired(actor="test", dry_run=False)
        with service.db.connect() as conn:
            assistant = conn.execute(
                "SELECT content, context_snapshot_id FROM messages WHERE id=?",
                (response.message_id,),
            ).fetchone()
            snapshot_count = conn.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0]
        assert report["context_snapshots_deleted"] == 2
        assert assistant["content"] == "[PURGED_BY_RETENTION]"
        assert assistant["context_snapshot_id"] is None
        assert snapshot_count == 0
    finally:
        service.close()
