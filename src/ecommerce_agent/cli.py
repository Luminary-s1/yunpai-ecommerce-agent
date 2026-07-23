from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from pydantic import ValidationError

from .config import Settings, is_loopback_host
from .disaster_recovery import (
    DisasterRecoveryError,
    DisasterRecoveryService,
    decode_backup_key,
    validate_key_id,
)
from .evals import run_offline_evaluation
from .llm import ModelError
from .releases import ReleaseError, ReleaseReplayRequest
from .service import AgentService
from .simulation import VirtualStoreSimulation


def _print_json(value: object, *, stream=None) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def _default_backup_path(settings: Settings) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return settings.resolved_backup_dir / f"yunpai-{timestamp}-{uuid.uuid4().hex[:12]}.ypbak"


def _run_disaster_recovery(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    recovery = DisasterRecoveryService()
    try:
        if args.command == "backup-prune":
            report = recovery.prune_backups(
                backup_dir=(args.backup_dir or settings.resolved_backup_dir),
                keep=args.keep,
                apply=args.apply,
            )
        elif args.command == "backup-rollback":
            report = recovery.rollback_restore(
                receipt_path=args.receipt,
                target_data_dir=(args.target_data_dir or settings.data_dir),
            )
        else:
            key = decode_backup_key(settings.backup_encryption_key)
            if args.command == "backup":
                report = recovery.create_backup(
                    data_dir=settings.data_dir,
                    output_path=(args.output or _default_backup_path(settings)),
                    master_key=key,
                    key_id=validate_key_id(settings.backup_key_id),
                    require_stopped=args.require_stopped,
                )
            elif args.command == "backup-verify":
                report = recovery.verify_backup(
                    archive_path=args.archive,
                    master_key=key,
                )
            elif args.command == "backup-restore":
                report = recovery.restore_backup(
                    archive_path=args.archive,
                    target_data_dir=(args.target_data_dir or settings.data_dir),
                    master_key=key,
                    force=args.force,
                )
            elif args.command == "backup-rekey":
                new_key = decode_backup_key(
                    os.getenv("BACKUP_NEW_ENCRYPTION_KEY", ""),
                    variable="BACKUP_NEW_ENCRYPTION_KEY",
                )
                new_key_id = validate_key_id(
                    os.getenv("BACKUP_NEW_KEY_ID", ""),
                    variable="BACKUP_NEW_KEY_ID",
                )
                report = recovery.rekey_backup(
                    archive_path=args.archive,
                    output_path=args.output,
                    old_master_key=key,
                    new_master_key=new_key,
                    new_key_id=new_key_id,
                )
            else:
                raise DisasterRecoveryError("unsupported disaster recovery command")
    except DisasterRecoveryError as exc:
        _print_json({"ok": False, "error": str(exc)}, stream=sys.stderr)
        raise SystemExit(1) from exc
    _print_json(report)


def main() -> None:
    parser = argparse.ArgumentParser(description="云湃电商客服 Agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="启动 REST API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)

    chat = subparsers.add_parser("chat", help="发送单轮测试消息")
    chat.add_argument("message")
    chat.add_argument("--session", default="cli-session")

    subparsers.add_parser("init", help="初始化数据库并显示健康状态")
    subparsers.add_parser("eval", help="运行离线信任边界、检索与安全评测")
    simulation = subparsers.add_parser(
        "simulate-store", help="导入内置虚拟店铺并执行跨模块运营场景验收"
    )
    simulation.add_argument(
        "--skip-customer-service",
        action="store_true",
        help="只验收经营数据模块，不创建客服会话和人工任务",
    )
    subparsers.add_parser("model-probe", help="执行一次最小真实模型连通性测试")
    retention = subparsers.add_parser("retention", help="检查或执行数据留存清理")
    retention.add_argument("--apply", action="store_true", help="实际执行；默认只预览")
    release_replay = subparsers.add_parser(
        "release-replay", help="在隔离快照中执行版本化发布回放门禁"
    )
    release_replay.add_argument("release_id")
    release_replay.add_argument(
        "cases", type=Path, help="UTF-8 JSON 文件，结构为 {\"cases\": [...]}"
    )

    backup = subparsers.add_parser("backup", help="创建经过认证加密的数据库快照集")
    backup.add_argument("--output", type=Path, help="归档输出路径；默认写入 BACKUP_DIR")
    backup.add_argument(
        "--require-stopped",
        action="store_true",
        help="要求服务已停止并持有运行锁，适合升级或正式恢复前备份",
    )

    backup_verify = subparsers.add_parser("backup-verify", help="解密并完整校验备份")
    backup_verify.add_argument("archive", type=Path)

    backup_restore = subparsers.add_parser(
        "backup-restore", help="校验后恢复业务库和 checkpoint 库"
    )
    backup_restore.add_argument("archive", type=Path)
    backup_restore.add_argument("--target-data-dir", type=Path)
    backup_restore.add_argument(
        "--force",
        action="store_true",
        help="覆盖已有数据库；会保留独立回滚目录",
    )

    backup_rekey = subparsers.add_parser(
        "backup-rekey", help="使用 BACKUP_NEW_* 环境变量轮换归档密钥"
    )
    backup_rekey.add_argument("archive", type=Path)
    backup_rekey.add_argument("--output", type=Path, required=True)

    backup_rollback = subparsers.add_parser(
        "backup-rollback", help="根据恢复回执回滚并保留当前版本"
    )
    backup_rollback.add_argument("receipt", type=Path)
    backup_rollback.add_argument("--target-data-dir", type=Path)

    backup_prune = subparsers.add_parser(
        "backup-prune", help="预览或执行本地备份保留策略"
    )
    backup_prune.add_argument("--backup-dir", type=Path)
    backup_prune.add_argument("--keep", type=int, default=14)
    backup_prune.add_argument("--apply", action="store_true", help="实际删除；默认只预览")
    args = parser.parse_args()

    if args.command == "serve":
        settings = Settings.from_env()
        if not settings.admin_auth_required and not is_loopback_host(args.host):
            _print_json(
                {
                    "ok": False,
                    "error": (
                        "ADMIN_AUTH_REQUIRED=false is limited to a loopback --host "
                        "such as 127.0.0.1 or ::1"
                    ),
                },
                stream=sys.stderr,
            )
            raise SystemExit(2)
        uvicorn.run(
            "ecommerce_agent.api:create_app",
            host=args.host,
            port=args.port,
            reload=False,
            factory=True,
        )
        return

    if args.command in {
        "backup",
        "backup-verify",
        "backup-restore",
        "backup-rekey",
        "backup-rollback",
        "backup-prune",
    }:
        _run_disaster_recovery(args)
        return

    service = AgentService()
    try:
        if args.command == "chat":
            principal = service.auth.authenticate(
                service.settings.bootstrap_client_id,
                service.settings.bootstrap_client_key,
                "cli-user",
            )
            print(service.chat(principal, args.session, args.message).model_dump_json(indent=2))
        elif args.command == "init":
            _print_json(service.health())
        elif args.command == "eval":
            report = run_offline_evaluation(
                service.knowledge,
                tenant_id=service.settings.bootstrap_tenant_id,
            )
            _print_json(report)
            raise SystemExit(0 if report["passed"] else 1)
        elif args.command == "simulate-store":
            report = VirtualStoreSimulation(service).run(
                tenant_id=service.settings.bootstrap_tenant_id,
                actor=service.settings.bootstrap_admin_id,
                include_customer_service=not args.skip_customer_service,
            )
            _print_json(report)
            raise SystemExit(0 if report["passed"] else 2)
        elif args.command == "model-probe":
            try:
                report = service.model.probe()
            except ModelError as exc:
                _print_json(
                    {
                        "ok": False,
                        "provider": service.settings.model_provider,
                        "model": service.settings.model_name,
                        "error": str(exc),
                    }
                )
                raise SystemExit(1) from exc
            _print_json(report)
            raise SystemExit(0 if report["ok"] else 1)
        elif args.command == "retention":
            report = service.purge_expired(actor="cli", dry_run=not args.apply)
            _print_json(report)
        elif args.command == "release-replay":
            try:
                payload = json.loads(args.cases.read_text(encoding="utf-8"))
                request = ReleaseReplayRequest.model_validate(payload)
                report = service.run_release_replay(
                    service.settings.bootstrap_tenant_id,
                    args.release_id,
                    request,
                    service.settings.bootstrap_admin_id,
                )
            except (OSError, json.JSONDecodeError):
                _print_json(
                    {"ok": False, "error": "release replay dataset cannot be read"},
                    stream=sys.stderr,
                )
                raise SystemExit(1)
            except ValidationError:
                _print_json(
                    {"ok": False, "error": "release replay dataset is invalid"},
                    stream=sys.stderr,
                )
                raise SystemExit(1)
            except ReleaseError as exc:
                _print_json({"ok": False, "error": str(exc)}, stream=sys.stderr)
                raise SystemExit(1) from exc
            _print_json(report)
            raise SystemExit(0 if report["passed"] else 2)
    finally:
        service.close()


if __name__ == "__main__":
    main()
