"""知识引擎可观测性：检索统计 + trace 记录 + 轻量指标。

设计（对齐项目 trace 机制：AgentState.trace 为 list[str] 轻量轨迹）：
- 本模块把"检索链路发生了什么"沉淀为结构化统计，供日志/报告/运维观察。
- 与 trace 分离：trace 走轻量字符串（进 AgentState），统计走本模块（可持久化/聚合）。
- 零第三方依赖；内存计数器 + 可选 SQLite 持久化（P0-2：检索日志重启不丢）。

用法：
    from .observability import RetrievalObserver
    obs = RetrievalObserver(db)   # 传 db 启用持久化（record/report 均走 SQLite）
    obs.record_search(tenant=..., store=..., query=..., hits=..., guard_blocks=...)
    obs.report()  # 返回聚合统计（含持久化历史）
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("knowledge_engine.observability")


@dataclass
class SearchRecord:
    """一次检索的统计记录。"""

    ts: float
    tenant_id: str
    store_id: str
    query: str = ""
    hits: int = 0
    guard_blocks: int = 0          # 护栏拦截的条目数
    guard_scope_block: bool = False  # 意图门拦截
    memory_recalled: int = 0       # 记忆召回的条数
    latency_ms: float = 0.0
    failed: bool = False           # 检索是否失败（B 修复：失败可观测）
    source: str = "graph"          # graph / graph_refine / graph_api
    event_type: str = "normal"     # P1 事件分类：normal / scope_escalate / scope_block / failure

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "tenant_id": self.tenant_id,
            "store_id": self.store_id,
            "query": self.query[:120],
            "hits": self.hits,
            "guard_blocks": self.guard_blocks,
            "guard_scope_block": self.guard_scope_block,
            "memory_recalled": self.memory_recalled,
            "latency_ms": round(self.latency_ms, 2),
            "failed": self.failed,
            "source": self.source,
            "event_type": self.event_type,
        }


class RetrievalObserver:
    """检索观测器：记录检索事件，提供聚合统计。

    P0-2 增强：传入 db 时启用 SQLite 持久化——
    record_search 写入 retrieval_logs 表，report 聚合全量历史（重启不丢）。
    未传 db 时退化为纯内存模式（原行为，兼容单测/无库环境）。
    """

    def __init__(self, keep_last: int = 5000, db: Any = None) -> None:
        self._records: list[SearchRecord] = []
        self._keep_last = keep_last
        self._lock = threading.Lock()
        self._db = db

    @staticmethod
    def _to_row(rec: SearchRecord) -> tuple[Any, ...]:
        return (
            rec.ts, rec.tenant_id, rec.store_id, rec.query[:120], rec.hits,
            rec.guard_blocks, int(rec.guard_scope_block), rec.memory_recalled,
            round(rec.latency_ms, 2), int(rec.failed), rec.source, rec.event_type,
        )

    def record_search(self, **kwargs: Any) -> None:
        """记录一次检索。kwargs 对应 SearchRecord 字段。"""
        # P1 事件分类：未显式指定时按语义自动归类
        if "event_type" not in kwargs:
            if kwargs.get("failed"):
                kwargs["event_type"] = "failure"
            elif kwargs.get("guard_scope_block"):
                kwargs["event_type"] = "scope_block"
            elif kwargs.get("guard_blocks", 0) > 0:
                kwargs["event_type"] = "guard_block"
            else:
                kwargs["event_type"] = "normal"
        rec = SearchRecord(ts=time.time(), tenant_id=kwargs.pop("tenant_id", ""),
                           store_id=kwargs.pop("store_id", ""), **kwargs)
        with self._lock:
            self._records.append(rec)
            if len(self._records) > self._keep_last:
                self._records = self._records[-self._keep_last:]
            if self._db is not None:
                try:
                    with self._db._write_lock, self._db.connect() as conn:
                        conn.execute(
                            """
                            INSERT INTO retrieval_logs(
                                ts, tenant_id, store_id, query, hits, guard_blocks,
                                guard_scope_block, memory_recalled, latency_ms,
                                failed, source, event_type
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            self._to_row(rec),
                        )
                except Exception:
                    # 持久化失败不阻塞检索主链路（可观测性降级为内存模式）
                    logger.exception("retrieval_logs persist failed")

    def _all_records(self) -> list[SearchRecord]:
        """返回全部记录：持久化模式读库合并内存（防丢），内存模式返回列表副本。"""
        if self._db is None:
            return list(self._records)
        rows: list[SearchRecord] = []
        try:
            with self._db.connect() as conn:
                for row in conn.execute(
                    """
                    SELECT ts, tenant_id, store_id, query, hits, guard_blocks,
                           guard_scope_block, memory_recalled, latency_ms,
                           failed, source, event_type
                    FROM retrieval_logs ORDER BY ts DESC LIMIT ?
                    """,
                    (max(self._keep_last * 4, 20000),),
                ):
                    rows.append(
                        SearchRecord(
                            ts=row["ts"], tenant_id=row["tenant_id"],
                            store_id=row["store_id"], query=row["query"],
                            hits=row["hits"], guard_blocks=row["guard_blocks"],
                            guard_scope_block=bool(row["guard_scope_block"]),
                            memory_recalled=row["memory_recalled"],
                            latency_ms=row["latency_ms"],
                            failed=bool(row["failed"]),
                            source=row["source"], event_type=row["event_type"],
                        )
                    )
        except Exception:
            logger.exception("retrieval_logs read failed")
            return list(self._records)
        return rows

    def report(self, *, recent_window_seconds: float = 3600.0) -> dict[str, Any]:
        """返回聚合统计（观测窗口内的检索健康度）。

        P1 增强：
        - 事件分类明细（normal/scope_block/guard_block/failure），识别攻击/滥用趋势
        - 最近 1h 窗口统计（时间新鲜度，避免长期陈旧）
        P0-2 增强：持久化模式下聚合含磁盘历史（重启不丢）。
        """
        records = self._all_records()
        n = len(records)
        now = time.time()
        recent = [r for r in records if now - r.ts <= recent_window_seconds]
        if n == 0:
            return {"searches": 0, "avg_hits": 0.0, "avg_latency_ms": 0.0,
                    "guard_blocks_total": 0, "scope_blocks_total": 0,
                    "memory_recalls_total": 0, "failures_total": 0,
                    "actual_retrievals": 0, "by_event_type": {},
                    "recent_1h": {"searches": 0, "failures": 0, "scope_blocks": 0, "guard_blocks": 0}}
        # B 修复：聚合口径区分"真实检索"与"意图门拦截"（guard_scope_block 的 0 命中不应拉低 avg_hits）
        real = [r for r in records if not r.guard_scope_block]
        if real:
            avg_hits = sum(r.hits for r in real) / len(real)
            avg_lat = sum(r.latency_ms for r in real) / len(real)
        else:
            avg_hits = 0.0
            avg_lat = 0.0
        # P1 事件分类统计
        by_type: dict[str, int] = {}
        for r in records:
            by_type[r.event_type] = by_type.get(r.event_type, 0) + 1
        return {
            "searches": n,
            "avg_hits": round(avg_hits, 2),
            "avg_latency_ms": round(avg_lat, 2),
            "guard_blocks_total": sum(r.guard_blocks for r in records),
            "scope_blocks_total": sum(1 for r in records if r.guard_scope_block),
            "memory_recalls_total": sum(r.memory_recalled for r in records),
            "failures_total": sum(1 for r in records if r.failed),
            "actual_retrievals": len(real),
            "by_event_type": by_type,
            "recent_1h": {
                "searches": len(recent),
                "failures": sum(1 for r in recent if r.failed),
                "scope_blocks": sum(1 for r in recent if r.guard_scope_block),
                # R6 修复：注入/敏感拦截趋势也进新鲜度视图
                "guard_blocks": sum(r.guard_blocks for r in recent),
            },
        }

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            if self._db is not None:
                try:
                    with self._db.connect() as conn:
                        conn.execute("DELETE FROM retrieval_logs")
                except Exception:
                    logger.exception("retrieval_logs clear failed")


# 全局观测器（服务级共享）
_observer: RetrievalObserver | None = None
_observer_lock = threading.Lock()


def get_observer(db: Any = None) -> RetrievalObserver:
    """获取全局观测器单例。

    P0-2：首次创建时传入 db 可启用持久化；已有单例返回原实例。
    """
    global _observer
    with _observer_lock:
        if _observer is None:
            _observer = RetrievalObserver(db=db)
        return _observer
