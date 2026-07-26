from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys

from ecommerce_agent.database import Database
from ecommerce_agent.service import AgentService
from ecommerce_agent.releases import ReleasePolicyCreateRequest

from conftest import make_settings, principal_for


def test_module_cli_entrypoint() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    result = subprocess.run(
        [sys.executable, "-m", "ecommerce_agent.cli", "--help"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "serve" in result.stdout
    assert "backup-restore" in result.stdout
    assert "backup-rollback" in result.stdout
    assert "release-replay" in result.stdout


def _run_cli(arguments: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ecommerce_agent.cli", *arguments],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_backup_cli_create_verify_rekey_and_restore(tmp_path) -> None:
    source_dir = tmp_path / "source"
    service = AgentService(make_settings(source_dir))
    try:
        service.chat(principal_for(service), "cli-backup", "退货政策是什么？")
    finally:
        service.close()

    archive = tmp_path / "yunpai-cli.ypbak"
    rotated = tmp_path / "yunpai-cli-rotated.ypbak"
    restored_dir = tmp_path / "restored"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "src",
            "PYTHONUTF8": "1",
            "DATA_DIR": str(source_dir),
            "BACKUP_DIR": str(tmp_path / "backups"),
            "BACKUP_KEY_ID": "cli-v1",
            "BACKUP_ENCRYPTION_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        }
    )

    created = _run_cli(["backup", "--output", str(archive)], env)
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["key_id"] == "cli-v1"
    verified = _run_cli(["backup-verify", str(archive)], env)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["schema_version"] == Database.SCHEMA_VERSION

    env["BACKUP_NEW_KEY_ID"] = "cli-v2"
    env["BACKUP_NEW_ENCRYPTION_KEY"] = "Hx4dHBsaGRgXFhUUExIREA8ODQwLCgkIBwYFBAMCAQA="
    rekeyed = _run_cli(
        ["backup-rekey", str(archive), "--output", str(rotated)],
        env,
    )
    assert rekeyed.returncode == 0, rekeyed.stderr
    assert json.loads(rekeyed.stdout)["new_key_id"] == "cli-v2"

    restored = _run_cli(
        [
            "backup-restore",
            str(archive),
            "--target-data-dir",
            str(restored_dir),
        ],
        env,
    )
    assert restored.returncode == 0, restored.stderr
    assert Path(json.loads(restored.stdout)["receipt"]).is_file()

    rollback_target = tmp_path / "rollback-target"
    rollback_service = AgentService(make_settings(rollback_target))
    rollback_service.close()
    force_restored = _run_cli(
        [
            "backup-restore",
            str(archive),
            "--target-data-dir",
            str(rollback_target),
            "--force",
        ],
        env,
    )
    assert force_restored.returncode == 0, force_restored.stderr
    force_report = json.loads(force_restored.stdout)
    rolled_back = _run_cli(
        [
            "backup-rollback",
            force_report["receipt"],
            "--target-data-dir",
            str(rollback_target),
        ],
        env,
    )
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert Path(json.loads(rolled_back.stdout)["forward_directory"]).is_dir()

    wrong_env = dict(env)
    wrong_env["BACKUP_ENCRYPTION_KEY"] = env["BACKUP_NEW_ENCRYPTION_KEY"]
    wrong = _run_cli(["backup-verify", str(archive)], wrong_env)
    assert wrong.returncode == 1
    error = json.loads(wrong.stderr)
    assert error["ok"] is False
    assert "authentication failed" in error["error"]
    assert "Traceback" not in wrong.stderr


def test_release_replay_cli_runs_isolated_gate_and_redacts_validation_errors(tmp_path) -> None:
    source_dir = tmp_path / "release-source"
    service = AgentService(make_settings(source_dir))
    try:
        release = service.releases.create(
            "tenant-test",
            ReleasePolicyCreateRequest(
                release_key="cli.reply",
                name="CLI 影子回放",
                platform="taobao",
                store_id="store-a",
                mode="shadow",
                traffic_percentage=100,
                intent_allowlist=["product"],
                min_replay_cases=1,
                max_replay_failure_rate=0,
                max_replay_severe_errors=0,
            ),
            "admin-test",
        )
        disabled_release = service.releases.create(
            "tenant-test",
            ReleasePolicyCreateRequest(
                release_key="cli.disabled-model",
                name="CLI 禁用模型回放",
                platform="taobao",
                store_id="store-a",
                mode="shadow",
                traffic_percentage=100,
                intent_allowlist=["product"],
                min_replay_cases=1,
                max_replay_failure_rate=0,
                max_replay_severe_errors=0,
            ),
            "admin-test",
        )
    finally:
        service.close()

    cases = tmp_path / "release-cases.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "cli-product",
                        "message": "尺码怎么选",
                        "context": {},
                        "expectation": {
                            "expected_intent": "product",
                            "expected_requires_human": False,
                            "require_sources": True,
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "src",
            "PYTHONUTF8": "1",
            "DATA_DIR": str(source_dir),
            "BOOTSTRAP_TENANT_ID": "tenant-test",
            "BOOTSTRAP_ADMIN_ID": "admin-test",
            "MODEL_ENABLED": "false",
            "MODEL_MOCK_MODE": "true",
        }
    )
    result = _run_cli(
        ["release-replay", release["id"], str(cases)], env
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["passed"] is True
    assert report["summary"]["total"] == 1

    invalid = tmp_path / "invalid-cases.json"
    invalid.write_text(
        '{"cases":[{"case_id":"bad","message":"private raw text"}]}',
        encoding="utf-8",
    )
    rejected = _run_cli(
        ["release-replay", release["id"], str(invalid)], env
    )
    assert rejected.returncode == 1
    assert json.loads(rejected.stderr)["error"] == "release replay dataset is invalid"
    assert "private raw text" not in rejected.stderr
    assert "Traceback" not in rejected.stderr

    disabled_env = dict(env)
    disabled_env["MODEL_MOCK_MODE"] = "false"
    blocked = _run_cli(
        ["release-replay", disabled_release["id"], str(cases)], disabled_env
    )
    assert blocked.returncode == 2
    blocked_report = json.loads(blocked.stdout)
    assert blocked_report["passed"] is False
    assert "model_fallback_disallowed" in blocked_report["results"][0]["violations"]
