"""P0-2 检索日志落 SQLite：观察器持久化后重启不丢，聚合含磁盘历史。"""

from __future__ import annotations

from pathlib import Path

from ecommerce_agent.database import Database
from ecommerce_agent.knowledge_engine.observability import RetrievalObserver


def test_persist_and_reload_after_new_observer(tmp_path: Path) -> None:
    """检索日志落库：重建 observer（模拟重启）后 report 仍含历史。"""
    db = Database(tmp_path / "obs.sqlite3")
    db.initialize()
    obs = RetrievalObserver(db=db)
    obs.record_search(
        tenant_id="tenant-a", store_id="store-a", query="尺码怎么选",
        hits=3, latency_ms=12.3,
    )
    obs.record_search(
        tenant_id="tenant-a", store_id="store-a", query="退款流程",
        hits=1, failed=True, latency_ms=5.0,
    )

    # 模拟进程重启：同一 DB，全新 observer（不读内存）
    obs2 = RetrievalObserver(db=db)
    report = obs2.report(recent_window_seconds=3600)
    assert report["searches"] == 2
    assert report["failures_total"] == 1
    assert report["by_event_type"]["failure"] == 1
    assert report["actual_retrievals"] == 2

    # 表里确实有 2 行
    with db.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM retrieval_logs").fetchone()[0]
    assert rows == 2


def test_memory_only_mode_when_no_db(tmp_path: Path) -> None:
    """无 db 时退化为纯内存模式（原行为不破坏）。"""
    obs = RetrievalObserver()
    obs.record_search(tenant_id="t", store_id="s", query="q", hits=1)
    assert obs.report()["searches"] == 1


def test_clear_removes_persisted_rows(tmp_path: Path) -> None:
    """clear 同时清空持久化行。"""
    db = Database(tmp_path / "obs2.sqlite3")
    db.initialize()
    obs = RetrievalObserver(db=db)
    obs.record_search(tenant_id="t", store_id="s", query="q", hits=1)
    obs.clear()
    assert obs.report()["searches"] == 0
    with db.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) FROM retrieval_logs").fetchone()[0]
    assert rows == 0
