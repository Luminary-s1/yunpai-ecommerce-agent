"""knowledge_engine 梦循环调度器：让三作业按计划自动运行。

设计（低耦合、可复用、零第三方依赖）：
- 用 threading.Timer 实现定时循环（标准库，无外部依赖）
- 三个作业可独立配置频率（默认：摄取每天、一致性每天、合并每周）
- 支持一次性运行（run_once）供 cron / 系统计划任务 / 手动触发
- 独立运行，不侵入运行时；状态通过日志/返回值暴露

用法：
    # 一次性运行（可配 cron / 计划任务每天触发）
    python -m ecommerce_agent.knowledge_engine.scheduler --once

    # 常驻循环（默认间隔）
    python -m ecommerce_agent.knowledge_engine.scheduler
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from .dream_cycle import ingest, consistency_check, consolidate, auto_repair
from .loader import load_clean_dir

logger = logging.getLogger("knowledge_engine.scheduler")

# 默认运行间隔（秒）：摄取 1 天、一致性 1 天、合并记忆 7 天
DEFAULT_INTERVALS = {
    "ingest": 86400,       # 24h
    "consistency": 86400,  # 24h
    "consolidate": 604800, # 7d
}

# 任务6产物路径（相对项目根）
DEFAULT_CLEAN_DIR = "knowledge_graph_output/02_clean"


def run_dream_cycle_once(
    clean_dir: str | Path = DEFAULT_CLEAN_DIR,
    *,
    min_facts: int = 3,
    threshold: float = 0.85,
    persist: bool = False,
    knowledge_base=None,
) -> dict:
    """一次性跑完整梦循环（加载 + 三作业），返回报告。

    persist=True 时把合并记忆结论落库（apply_consolidation，P1-1 闭环）。
    """
    items = load_clean_dir(clean_dir)
    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "total_items": len(items),
        "ingest": {"new": 0, "duplicates": 0},
        "consistency": {"dangling_references": 0, "orphan_nodes": 0},
        "auto_repair": {"marked_dangling": 0, "marked_orphan": 0},
        "consolidate": {"clusters": 0, "consolidated": 0, "skipped": 0},
    }

    # 增量摄取（现有 ids 为 0，视为首次全量）
    ingest_report = ingest(items, existing_ids=[])
    report["ingest"] = {
        "new": len(ingest_report.new_items),
        "duplicates": ingest_report.duplicates,
    }

    # 一致性校验
    consistency = consistency_check(items)
    report["consistency"] = {
        "dangling_references": len(consistency.dangling_references),
        "orphan_nodes": len(consistency.orphan_nodes),
    }

    # 自动修复：标记悬空引用 + 孤立节点（不删数据，可溯源）
    repair = auto_repair(consistency, items)
    report["auto_repair"] = {
        "marked_dangling": repair["marked_dangling"],
        "marked_orphan": repair["marked_orphan"],
    }

    # 合并记忆
    cons = consolidate(items, min_facts=min_facts, threshold=threshold)
    report["consolidate"] = {
        "clusters": len(cons.clusters),
        "consolidated": len(cons.consolidated),
        "skipped": cons.skipped,
    }
    # P1-1：合并结论落库（persist=True 且提供了 knowledge_base）
    if persist and knowledge_base is not None:
        from .dream_cycle import apply_consolidation

        persist_stats = apply_consolidation(cons, knowledge_base)
        report["consolidate"]["persisted"] = persist_stats["written"]
        report["consolidate"]["persist_skipped"] = persist_stats["skipped_existing"]
    # 结构化日志（运维-1：key=value，含时间戳）
    logger.info(
        "dream_cycle done: total_items=%d ingest_new=%d ingest_dup=%d "
        "dangling=%d orphan=%d repaired=%d clusters=%d consolidated=%d",
        report["total_items"],
        report["ingest"]["new"],
        report["ingest"]["duplicates"],
        report["consistency"]["dangling_references"],
        report["consistency"]["orphan_nodes"],
        report["auto_repair"]["marked_dangling"] + report["auto_repair"]["marked_orphan"],
        report["consolidate"]["clusters"],
        report["consolidate"]["consolidated"],
    )
    return report


def _log(msg: str) -> None:
    print(f"[dream-cycle] {datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def run_loop(clean_dir: str | Path, intervals: dict[str, int] | None = None) -> None:
    """常驻循环：按间隔定时跑三作业。Ctrl+C 停止。"""
    intervals = intervals or DEFAULT_INTERVALS
    _log(f"梦循环启动（clean_dir={clean_dir}），间隔={intervals}")
    last_run = {k: 0.0 for k in intervals}

    while True:
        now = time.time()
        for job, interval in intervals.items():
            if now - last_run[job] >= interval:
                last_run[job] = now
                try:
                    _log(f"开始作业: {job}")
                    report = run_dream_cycle_once(clean_dir)
                    _log(f"作业 {job} 完成: {report[job]}")
                except Exception as exc:
                    _log(f"作业 {job} 失败: {exc}")
        time.sleep(60)  # 每分钟检查一次


def run_evaluation_once(*, verbose: bool = False) -> dict:
    """跑一次检索质量评测（35 题），返回报告（可观测-1：持续监控）。

    通过率下降时（< RAG_EVAL_THRESHOLD 环境变量，默认 0.9）返回 error 状态：
    - report["status"] = "below_threshold" / "ok"
    - report["error"] = 门禁告警消息（供监控/飞书/CI 捕获）
    无 Neo4j 环境：返回降级报告（error 字段），不抛栈——调用方可据此跳过。
    """
    import os

    from .graph_retrieval import GraphRetrievalService
    from .neo4j_client import Neo4jClient

    # P2-2 评测阈值门禁：环境变量可配置，低于阈值不再只记日志
    try:
        eval_threshold = float(os.environ.get("RAG_EVAL_THRESHOLD", "0.9"))
    except ValueError:
        eval_threshold = 0.9
        logger.warning("RAG_EVAL_THRESHOLD 非数值，回落默认 0.9")

    try:
        svc = GraphRetrievalService(Neo4jClient())
    except Exception:
        svc = None
    from .evaluation_suite import run_evaluation

    if svc is None:
        degraded = {
            "total": 0,
            "passed": 0,
            "pass_rate": 0.0,
            "status": "skipped",
            "error": "Neo4j 不可用，评测跳过",
        }
        logger.warning("retrieval_eval skipped: Neo4j unavailable")
        return degraded
    try:
        report = run_evaluation(svc, verbose=verbose)
    except Exception as exc:
        logger.warning("retrieval_eval failed: %s", exc)
        return {
            "total": 0,
            "passed": 0,
            "pass_rate": 0.0,
            "status": "error",
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    pass_rate = report["pass_rate"]
    logger.info(
        "retrieval_eval done: total=%d passed=%d pass_rate=%.3f",
        report["total"],
        report["passed"],
        pass_rate,
    )
    if pass_rate < eval_threshold:
        # P2-2 门禁：低于阈值 = 评测未通过（error 状态 + 结构化告警载荷）
        report["status"] = "below_threshold"
        report["error"] = (
            f"retrieval_eval gate: pass_rate={pass_rate:.3f} "
            f"< RAG_EVAL_THRESHOLD={eval_threshold:.3f}"
        )
        report["eval_threshold"] = eval_threshold
        logger.error(
            "retrieval_eval REGRESSION GATE: pass_rate=%.3f < threshold=%.3f "
            "(failing=%s)",
            pass_rate,
            eval_threshold,
            [d for d in report["details"] if not d["passed"]][:10],
        )
    else:
        report["status"] = "ok"
        report["eval_threshold"] = eval_threshold
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="云湃知识库梦循环调度器")
    parser.add_argument("--once", action="store_true", help="一次性运行后退出（可配 cron）")
    parser.add_argument("--clean-dir", default=DEFAULT_CLEAN_DIR, help="任务6产物目录")
    parser.add_argument("--eval", action="store_true", help="跑一次检索质量评测（35 题）")
    parser.add_argument("--persist", action="store_true", help="合并记忆结论落库（需 --db）")
    parser.add_argument("--db", default="", help="运行时 SQLite 路径（--persist 时需要）")
    parser.add_argument("--ingest-interval", type=int, default=DEFAULT_INTERVALS["ingest"])
    parser.add_argument("--consistency-interval", type=int, default=DEFAULT_INTERVALS["consistency"])
    parser.add_argument("--consolidate-interval", type=int, default=DEFAULT_INTERVALS["consolidate"])
    args = parser.parse_args()

    if args.eval:
        report = run_evaluation_once(verbose=True)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if args.once:
        kb = None
        if args.persist:
            from ..database import Database
            from ..rag import KnowledgeBase

            if not args.db:
                print("--persist 需要 --db <sqlite路径>", file=sys.stderr)
                return 1
            db = Database(Path(args.db))
            db.initialize()
            kb = KnowledgeBase(db)
        report = run_dream_cycle_once(args.clean_dir, persist=args.persist, knowledge_base=kb)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    intervals = {
        "ingest": args.ingest_interval,
        "consistency": args.consistency_interval,
        "consolidate": args.consolidate_interval,
    }
    run_loop(args.clean_dir, intervals)


if __name__ == "__main__":
    main()
