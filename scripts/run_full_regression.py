#!/usr/bin/env python3
"""全量回归测试 — 高鲁棒性版。

目的：执行 `pytest tests`，杜绝 CI/CD 场景下的静默失败。每个 WP 合入前跑一次，
把结果（命令/返回码/输出/诊断/报告路径）以 JSON 持久化，供负责人/验收人核对。

用法：
    python scripts/run_full_regression.py              # 工作区必须干净（默认）
    python scripts/run_full_regression.py --allow-dirty # 允许未提交改动（WP 开发中验证用）

输出：
    pytest_debug_report.json（项目根，强制写入）+ 控制台报告

四原则落实声明：
1. 明确边界：输入=无（自动探测项目根）；输出=控制台 + JSON 报告；副作用=一次 pytest
   subprocess + 写报告文件。
2. 可观测信号：返回码、stdout/stderr、报告文件绝对路径、诊断快照（内存/磁盘/Python/pytest）。
3. 确定性自检：pre-flight（pytest>=7.0、目录可写、空间>100MB）+ 空输出 fallback 诊断。
4. 失败快速暴露：TimeoutExpired/PermissionError/FileNotFoundError 显式捕获+快照；
   空输出→OOM 检测 fallback；报告文件强制写入。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 防御措施：捕获异常时同时收集系统上下文快照，区分「代码问题 vs 环境问题」。
def _snapshot() -> dict[str, object]:
    """系统上下文快照（确定性字段；内存/磁盘为运行时刻点值）。

    跨平台内存采集：os.sysconf 是 Unix-only（Windows 抛 AttributeError）；
    优先用 psutil（若有），否则回退 0/None。磁盘用 shutil.disk_usage（跨平台）。
    """
    mem = None
    try:
        import psutil  # type: ignore[import-not-found]  # 可选依赖，无则回退

        mem = psutil.virtual_memory().available
    except Exception:  # noqa: BLE001  # psutil 未装或不可用
        try:
            mem = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (AttributeError, ValueError, OSError):
            mem = None
    disk = None
    try:
        disk = shutil.disk_usage(str(Path.cwd()))
    except OSError:
        pass
    return {
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "pytest_version": _pytest_version(),
        "platform": platform.platform(),
        "cwd": str(Path.cwd()),
        "mem_avail_bytes": mem,
        "disk_free_bytes": disk.free if disk else None,
        "disk_total_bytes": disk.total if disk else None,
        "git_branch": _git_branch(),
        "git_head": _git_head(),
        "dirty_files": len(_git_dirty()),
    }


def _pytest_version() -> str | None:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=30, env=_child_env(),
        )
        m = re.search(r"(\d+\.\d+\.\d+)", proc.stdout or "")
        return m.group(1) if m else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _git_branch() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True, text=True,
            encoding="utf-8", timeout=15,
        )
        return (proc.stdout or "").strip() or None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _git_head() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", "-1"], capture_output=True, text=True,
            encoding="utf-8", timeout=15,
        )
        return (proc.stdout or "").strip() or None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _git_dirty() -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--short"], capture_output=True, text=True,
            encoding="utf-8", timeout=15,
        )
        return [line for line in (proc.stdout or "").splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, OSError):
        return []


def _child_env() -> dict[str, str]:
    """注入 PYTHONUNBUFFERED=1 实时输出 + PYTHONDONTWRITEBYTECODE=1 防缓存干扰。"""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _preflight() -> list[str]:
    """前置检查：pytest>=7.0、目录可写、空间>100MB。不满足→人类可读错误。"""
    errors: list[str] = []
    version = _pytest_version()
    if version is None:
        errors.append("pytest 不可用（找不到或执行失败）")
    else:
        try:
            major = int(version.split(".")[0])
            if major < 7:
                errors.append(f"pytest 版本 {version} < 7.0，中止")
        except (ValueError, IndexError):
            errors.append(f"无法解析 pytest 版本: {version!r}")
    cwd = Path.cwd()
    try:
        probe = cwd / ".regression_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        errors.append(f"当前目录不可写: {cwd}")
    try:
        disk = shutil.disk_usage(str(cwd))
        if disk.free < 100 * 1024 * 1024:
            errors.append(f"磁盘剩余 {disk.free} 字节 < 100MB")
    except OSError:
        errors.append("无法读取磁盘空间")
    return errors


def _fallback_diagnostics(stdout: str, stderr: str) -> list[str]:
    """Anti-Silent-Failure：stdout/stderr 均空时，收集诊断判断是代码还是环境问题。"""
    diag: list[str] = []
    if stdout.strip() or stderr.strip():
        return diag
    # OOM 检测：检查系统日志关键字（尽力而为，缺失不阻塞）
    for log_source, pattern in [
        ("/var/log/kern.log", r"Out of memory|Killed process"),
        ("/var/log/syslog", r"Out of memory|Killed process"),
    ]:
        path = Path(log_source)
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-50:]:
                    if re.search(pattern, line):
                        diag.append(f"可能 OOM: {log_source} 命中 {line.strip()}")
                        break
            except OSError:
                pass
    # pytest 可执行文件存在性 + 执行权限
    pytest_cmd = _pytest_cmd()
    if pytest_cmd is not None and not Path(pytest_cmd).exists():
        diag.append(f"pytest 可执行文件不存在: {pytest_cmd}")
    if not diag:
        diag.append("stdout/stderr 均为空且无 OOM 证据；请检查子进程是否被外部终止")
    return diag


def _pytest_cmd() -> str | None:
    """定位 pytest 可执行路径（尽力而为；找不到返回 None）。"""
    try:
        which = shutil.which("pytest")
        return which
    except OSError:
        return None


def _parse_failed_test_nodes(stdout: str) -> list[str]:
    """从 pytest 输出解析失败测试节点（FAILED 行）。"""
    nodes: list[str] = []
    for line in (stdout or "").splitlines():
        m = re.search(r"FAILED (\S+)", line)
        if m and m.group(1) not in nodes:
            nodes.append(m.group(1))
    return nodes


def _base_for_attribution() -> str | None:
    """推荐回归对照 Base：HEAD 的父提交 SHA（干净时），否则 origin/main。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD~1"], capture_output=True, text=True,
            encoding="utf-8", timeout=15,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return (proc.stdout or "").strip()[:12]
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def main() -> int:
    allow_dirty = "--allow-dirty" in sys.argv

    # 0. 前置检查（确定性自检，不满足直接中止给人类可读错误）
    preflight_errors = _preflight()
    if preflight_errors:
        print("=== PRE-FLIGHT FAILED ===", file=sys.stderr)
        for error in preflight_errors:
            print(f"  - {error}", file=sys.stderr)
        report = {
            "timestamp": _dt.datetime.now().isoformat(),
            "command": "preflight-failed",
            "returncode": None,
            "stdout": "",
            "stderr": "\n".join(preflight_errors),
            "diagnostics": preflight_errors,
            "snapshot": _snapshot(),
            "report_path": str(Path.cwd() / "pytest_debug_report.json"),
        }
        (Path.cwd() / "pytest_debug_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 3

    # 1. 工作区状态（默认必须干净；--allow-dirty 跳过，但记录到报告）
    dirty = _git_dirty()
    if dirty and not allow_dirty:
        print("=== WORKTREE DIRTY ===", file=sys.stderr)
        print("工作区有未提交改动。开发中验证请加 --allow-dirty；合入前必须干净。", file=sys.stderr)
        for line in dirty[:20]:
            print(f"  {line}", file=sys.stderr)
        return 2

    # 执行 pytest：subprocess.run 显式 timeout + capture_output + encoding
    # 全量回归配置（2026-08-18 定稿）：
    #   xdist 已实测否决——-n auto 开 14 worker 跑 11 分钟未完成，测试负载以 SQLite
    #   文件 I/O + 种子加载为主，多进程 I/O 争抢无收益（甚至更慢）。
    #   → 定稿：单进程 + 缓存（移除 -p no:cacheprovider，.pytest_cache 提速收集）。
    #   全量回归接受单进程 10-20 分钟；按分层策略每个 WP 收口才跑 1 次（共 4 次）。
    cmd = [
        sys.executable, "-m", "pytest", "tests",
        "-q", "--no-header",
    ]
    print(f"=== RUN: {' '.join(cmd)} ===")
    started = _dt.datetime.now().isoformat()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            timeout=900, env=_child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        # text=True 时 exc.stdout/stderr 已是 str（encoding="utf-8"），无需 decode
        stdout = exc.stdout if isinstance(exc.stdout, str) else (
            exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
        )
        stderr = exc.stderr if isinstance(exc.stderr, str) else (
            exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        )
        diagnostics = _fallback_diagnostics(stdout, stderr)
        diagnostics.append("超时 900s（可能死锁或测试挂起）")
        _write_report(cmd, None, stdout, stderr, diagnostics, started)
        return 124
    except PermissionError as exc:
        diagnostics = [f"PermissionError: {exc}"]
        _write_report(cmd, None, "", str(exc), diagnostics, started)
        return 1
    except FileNotFoundError as exc:
        diagnostics = [f"FileNotFoundError: {exc}（pytest 或解释器不存在？）"]
        _write_report(cmd, None, "", str(exc), diagnostics, started)
        return 1

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    diagnostics = _fallback_diagnostics(stdout, stderr)
    # T3.9（P5，阻断5 修复）：回归归因必须 Base 对照复跑，禁止凭记忆判定"既有失败"。
    # returncode != 0 时强制提示：先在固定 Base（origin/main 或父提交）复跑失败用例，
    # 只有 "Base 通过 / Head 失败" 才算本 PR 引入的回归。
    if proc.returncode not in (0, None):
        failed_nodes = _parse_failed_test_nodes(stdout)
        base_hint = _base_for_attribution()
        diagnostics.append(
            "回归归因（P5 规范）：以下失败必须先在固定 Base 复跑对照——"
            "`git checkout <BASE_SHA> && pytest <失败用例>` 通过 / Head 失败 才算 PR 引入，"
            "禁止凭记忆/经验判定既有失败。"
        )
        if failed_nodes:
            diagnostics.append("失败用例: " + "; ".join(failed_nodes[:10]))
        if base_hint:
            diagnostics.append(f"建议 Base: {base_hint}")
    _write_report(cmd, proc.returncode, stdout, stderr, diagnostics, started)

    # 3. 控制台输出（capture 后回放，保证即使管道损坏也有可见输出）
    if stdout.strip():
        print(stdout[-4000:])
    if stderr.strip():
        print(stderr[-2000:], file=sys.stderr)
    if diagnostics:
        print("=== DIAGNOSTICS ===")
        for line in diagnostics:
            print(f"  - {line}")
    print(f"=== DONE returncode={proc.returncode} ===")
    return proc.returncode


def _write_report(
    cmd: list[str],
    returncode: int | None,
    stdout: str,
    stderr: str,
    diagnostics: list[str],
    started: str,
) -> None:
    """强制持久化：无论成败都写 pytest_debug_report.json。"""
    report_path = Path.cwd() / "pytest_debug_report.json"
    report = {
        "timestamp": started,
        "finished_at": _dt.datetime.now().isoformat(),
        "command": " ".join(cmd),
        "returncode": returncode,
        "stdout": stdout[-20000:],  # 截断防爆，完整输出在终端
        "stderr": stderr[-20000:],
        "diagnostics": diagnostics,
        "snapshot": _snapshot(),
        "report_path": str(report_path),
    }
    try:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"=== REPORT: {report_path} ===")
    except OSError as exc:
        print(f"!! 报告写入失败: {exc}", file=sys.stderr)
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
