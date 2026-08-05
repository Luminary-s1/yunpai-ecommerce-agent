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
import threading
import time
from datetime import datetime
from pathlib import Path

from .dream_cycle import ingest, consistency_check, consolidate, auto_repair
from .loader import load_clean_dir

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
) -> dict:
    """一次性跑完整梦循环（加载 + 三作业），返回报告。"""
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


def main() -> None:
    parser = argparse.ArgumentParser(description="云湃知识库梦循环调度器")
    parser.add_argument("--once", action="store_true", help="一次性运行后退出（可配 cron）")
    parser.add_argument("--clean-dir", default=DEFAULT_CLEAN_DIR, help="任务6产物目录")
    parser.add_argument("--ingest-interval", type=int, default=DEFAULT_INTERVALS["ingest"])
    parser.add_argument("--consistency-interval", type=int, default=DEFAULT_INTERVALS["consistency"])
    parser.add_argument("--consolidate-interval", type=int, default=DEFAULT_INTERVALS["consolidate"])
    args = parser.parse_args()

    if args.once:
        report = run_dream_cycle_once(args.clean_dir)
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    intervals = {
        "ingest": args.ingest_interval,
        "consistency": args.consistency_interval,
        "consolidate": args.consolidate_interval,
    }
    run_loop(args.clean_dir, intervals)


if __name__ == "__main__":
    main()
