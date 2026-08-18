from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import ecommerce_agent.disaster_recovery as disaster_recovery_module
from ecommerce_agent.database import Database
from ecommerce_agent.disaster_recovery import (
    FORMAT_VERSION,
    MAGIC,
    PREFIX,
    DisasterRecoveryError,
    DisasterRecoveryService,
    _encrypt_payload,
    _validate_header,
    _validate_manifest,
    decode_backup_key,
    read_backup_header,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


KEY_V1 = bytes(range(32))
KEY_V2 = bytes(reversed(range(32)))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_service(data_dir: Path) -> AgentService:
    service = AgentService(make_settings(data_dir))
    service.chat(
        principal_for(service),
        "disaster-recovery-session",
        "退货政策是什么？",
    )
    service.db.audit(
        "backup.test",
        "test",
        None,
        {"marker": "BACKUP_PLAINTEXT_MARKER"},
        "tenant-test",
    )
    return service


def test_online_encrypted_backup_verify_restore_and_runtime_lock(tmp_path) -> None:
    source_dir = tmp_path / "source"
    archive_path = tmp_path / "yunpai-online.ypbak"
    service = _seed_service(source_dir)
    recovery = DisasterRecoveryService()
    try:
        with pytest.raises(DisasterRecoveryError, match="second startup"):
            AgentService(make_settings(source_dir))
        created = recovery.create_backup(
            data_dir=source_dir,
            output_path=archive_path,
            master_key=KEY_V1,
            key_id="key-2026-01",
        )
        assert created["ok"] is True
        assert created["capture"]["orphan_checkpoint_threads"] == 0
        assert b"BACKUP_PLAINTEXT_MARKER" not in archive_path.read_bytes()
        assert read_backup_header(archive_path)["key_id"] == "key-2026-01"

        verified = recovery.verify_backup(
            archive_path=archive_path,
            master_key=KEY_V1,
        )
        assert verified["ok"] is True
        assert verified["schema_version"] == Database.SCHEMA_VERSION
        assert verified["capture"]["session_count"] == 1
        assert verified["capture"]["checkpoint_thread_count"] == 1

        with pytest.raises(DisasterRecoveryError, match="data directory is in use"):
            recovery.restore_backup(
                archive_path=archive_path,
                target_data_dir=source_dir,
                master_key=KEY_V1,
                force=True,
            )
        with pytest.raises(DisasterRecoveryError, match="data directory is in use"):
            recovery.create_backup(
                data_dir=source_dir,
                output_path=tmp_path / "offline-active.ypbak",
                master_key=KEY_V1,
                key_id="key-2026-01",
                require_stopped=True,
            )
    finally:
        service.close()

    offline_archive = tmp_path / "offline-stopped.ypbak"
    offline = recovery.create_backup(
        data_dir=source_dir,
        output_path=offline_archive,
        master_key=KEY_V1,
        key_id="key-2026-01",
        require_stopped=True,
    )
    assert offline["capture"]["mode"] == "offline_runtime_locked"

    restored_dir = tmp_path / "restored"
    restored = recovery.restore_backup(
        archive_path=archive_path,
        target_data_dir=restored_dir,
        master_key=KEY_V1,
        force=False,
    )
    assert restored["ok"] is True
    assert restored["rollback_directory"] is None
    receipt = json.loads(Path(restored["receipt"]).read_text(encoding="ascii"))
    assert receipt["archive_id"] == created["archive_id"]
    assert receipt["archive_sha256"] == _sha256(archive_path)

    restored_service = AgentService(replace(make_settings(restored_dir), data_dir=restored_dir))
    try:
        assert (
            restored_service.health()["database"]["schema_version"]
            == Database.SCHEMA_VERSION
        )
        with restored_service.db.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        restored_service.close()


def test_backup_rejects_wrong_key_and_tampering(tmp_path) -> None:
    source_dir = tmp_path / "source"
    service = _seed_service(source_dir)
    service.close()
    recovery = DisasterRecoveryService()
    archive_path = tmp_path / "yunpai-authenticated.ypbak"
    recovery.create_backup(
        data_dir=source_dir,
        output_path=archive_path,
        master_key=KEY_V1,
        key_id="key-v1",
    )

    with pytest.raises(DisasterRecoveryError, match="authentication failed"):
        recovery.verify_backup(archive_path=archive_path, master_key=KEY_V2)

    tampered_path = tmp_path / "yunpai-tampered.ypbak"
    tampered = bytearray(archive_path.read_bytes())
    tampered[len(tampered) // 2] ^= 0x01
    tampered_path.write_bytes(tampered)
    with pytest.raises(DisasterRecoveryError, match="authentication failed"):
        recovery.verify_backup(archive_path=tampered_path, master_key=KEY_V1)
    assert not list(tmp_path.glob(".yunpai-verify-*"))


def test_rekey_preserves_snapshot_and_invalidates_old_key(tmp_path) -> None:
    source_dir = tmp_path / "source"
    service = _seed_service(source_dir)
    service.close()
    recovery = DisasterRecoveryService()
    original_path = tmp_path / "yunpai-original.ypbak"
    rotated_path = tmp_path / "yunpai-rotated.ypbak"
    original = recovery.create_backup(
        data_dir=source_dir,
        output_path=original_path,
        master_key=KEY_V1,
        key_id="key-v1",
    )
    rotated = recovery.rekey_backup(
        archive_path=original_path,
        output_path=rotated_path,
        old_master_key=KEY_V1,
        new_master_key=KEY_V2,
        new_key_id="key-v2",
    )
    assert rotated["archive_id"] == original["archive_id"]
    assert rotated["source_key_id"] == "key-v1"
    assert rotated["new_key_id"] == "key-v2"
    assert recovery.verify_backup(
        archive_path=rotated_path,
        master_key=KEY_V2,
    )["capture"] == recovery.verify_backup(
        archive_path=original_path,
        master_key=KEY_V1,
    )["capture"]
    with pytest.raises(DisasterRecoveryError, match="authentication failed"):
        recovery.verify_backup(archive_path=rotated_path, master_key=KEY_V1)


def test_restore_requires_force_rejects_sidecars_and_rolls_back_failure(
    tmp_path,
    monkeypatch,
) -> None:
    source_dir = tmp_path / "source"
    source_service = _seed_service(source_dir)
    source_service.close()
    target_dir = tmp_path / "target"
    target_service = AgentService(make_settings(target_dir))
    target_service.db.audit("target.original", "test", None, {}, "tenant-test")
    target_service.close()

    recovery = DisasterRecoveryService()
    archive_path = tmp_path / "yunpai-rollback.ypbak"
    recovery.create_backup(
        data_dir=source_dir,
        output_path=archive_path,
        master_key=KEY_V1,
        key_id="key-v1",
    )
    original_hashes = {
        name: _sha256(target_dir / name)
        for name in ("agent.sqlite3", "checkpoints.sqlite3")
    }

    with pytest.raises(DisasterRecoveryError, match="already contains"):
        recovery.restore_backup(
            archive_path=archive_path,
            target_data_dir=target_dir,
            master_key=KEY_V1,
            force=False,
        )

    sidecar = target_dir / "agent.sqlite3-wal"
    sidecar.write_bytes(b"active")
    with pytest.raises(DisasterRecoveryError, match="sidecar"):
        recovery.restore_backup(
            archive_path=archive_path,
            target_data_dir=target_dir,
            master_key=KEY_V1,
            force=True,
        )
    sidecar.unlink()

    real_replace = os.replace
    failed = False

    def fail_checkpoint_install(source, target):
        nonlocal failed
        source_path = Path(source)
        target_path = Path(target)
        if (
            not failed
            and source_path.name.startswith(".checkpoints.sqlite3.restore-")
            and target_path == target_dir / "checkpoints.sqlite3"
        ):
            failed = True
            raise OSError("injected checkpoint install failure")
        return real_replace(source, target)

    monkeypatch.setattr(disaster_recovery_module.os, "replace", fail_checkpoint_install)
    with pytest.raises(DisasterRecoveryError, match="rolled back"):
        recovery.restore_backup(
            archive_path=archive_path,
            target_data_dir=target_dir,
            master_key=KEY_V1,
            force=True,
        )
    assert failed is True
    assert {
        name: _sha256(target_dir / name)
        for name in ("agent.sqlite3", "checkpoints.sqlite3")
    } == original_hashes
    assert not list(tmp_path.glob(".target-restore-rollback-*"))
    assert not list(target_dir.glob("restore-receipt-*.json"))


def test_successful_force_restore_retains_and_rolls_back_previous_set(tmp_path) -> None:
    source_dir = tmp_path / "source"
    source_service = _seed_service(source_dir)
    source_service.close()
    target_dir = tmp_path / "target"
    target_service = AgentService(make_settings(target_dir))
    target_service.db.audit("target.original", "test", None, {}, "tenant-test")
    target_service.close()
    original_hashes = {
        name: _sha256(target_dir / name)
        for name in ("agent.sqlite3", "checkpoints.sqlite3")
    }

    recovery = DisasterRecoveryService()
    archive_path = tmp_path / "yunpai-force.ypbak"
    recovery.create_backup(
        data_dir=source_dir,
        output_path=archive_path,
        master_key=KEY_V1,
        key_id="key-v1",
    )
    restored = recovery.restore_backup(
        archive_path=archive_path,
        target_data_dir=target_dir,
        master_key=KEY_V1,
        force=True,
    )
    assert Path(restored["rollback_directory"]).is_dir()
    assert {
        name: _sha256(Path(restored["rollback_directory"]) / name)
        for name in ("agent.sqlite3", "checkpoints.sqlite3")
    } == original_hashes

    rolled_back = recovery.rollback_restore(
        receipt_path=Path(restored["receipt"]),
        target_data_dir=target_dir,
    )
    assert rolled_back["ok"] is True
    assert {
        name: _sha256(target_dir / name)
        for name in ("agent.sqlite3", "checkpoints.sqlite3")
    } == original_hashes
    assert Path(rolled_back["forward_directory"]).is_dir()
    assert Path(rolled_back["rollback_receipt"]).is_file()
    assert not Path(restored["rollback_directory"]).exists()


def test_retention_is_dry_run_first_and_ignores_invalid_archives(tmp_path) -> None:
    source_dir = tmp_path / "source"
    service = _seed_service(source_dir)
    service.close()
    recovery = DisasterRecoveryService()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for index in range(3):
        recovery.create_backup(
            data_dir=source_dir,
            output_path=backup_dir / f"yunpai-{index}.ypbak",
            master_key=KEY_V1,
            key_id="key-v1",
        )
    invalid = backup_dir / "yunpai-invalid.ypbak"
    invalid.write_bytes(b"not-an-archive")

    preview = recovery.prune_backups(backup_dir=backup_dir, keep=1, apply=False)
    assert preview["dry_run"] is True
    assert len(preview["candidates"]) == 2
    assert preview["removed"] == []
    assert preview["invalid_archives_ignored"] == [invalid.name]
    assert len(list(backup_dir.glob("yunpai-*.ypbak"))) == 4

    applied = recovery.prune_backups(backup_dir=backup_dir, keep=1, apply=True)
    assert sorted(applied["removed"]) == sorted(preview["candidates"])
    assert invalid.exists()
    assert len([path for path in backup_dir.glob("yunpai-*.ypbak") if path != invalid]) == 1


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "is required"),
        ("YWJj", "exactly 32 bytes"),
        ("not base64!", "base64 or hexadecimal"),
    ],
)
def test_backup_key_validation_is_explicit(value: str, message: str) -> None:
    with pytest.raises(DisasterRecoveryError, match=message):
        decode_backup_key(value)


def test_archive_parsers_and_operator_guardrails_fail_closed(tmp_path) -> None:
    recovery = DisasterRecoveryService()
    truncated = tmp_path / "truncated.ypbak"
    truncated.write_bytes(b"short")
    with pytest.raises(DisasterRecoveryError, match="truncated"):
        read_backup_header(truncated)

    unsupported = tmp_path / "unsupported.ypbak"
    unsupported.write_bytes(PREFIX.pack(b"NOTABAK1", FORMAT_VERSION, 1) + b"{}")
    with pytest.raises(DisasterRecoveryError, match="unsupported"):
        read_backup_header(unsupported)

    invalid_header = tmp_path / "invalid-header.ypbak"
    invalid_header.write_bytes(PREFIX.pack(MAGIC, FORMAT_VERSION, 1) + b"{")
    with pytest.raises(DisasterRecoveryError, match="header is invalid"):
        read_backup_header(invalid_header)

    with pytest.raises(DisasterRecoveryError, match="does not exist"):
        recovery.verify_backup(
            archive_path=tmp_path / "missing.ypbak",
            master_key=KEY_V1,
        )
    existing_output = tmp_path / "existing.ypbak"
    existing_output.write_bytes(b"preserve")
    with pytest.raises(DisasterRecoveryError, match="already exists"):
        recovery.create_backup(
            data_dir=tmp_path / "missing-data",
            output_path=existing_output,
            master_key=KEY_V1,
            key_id="key-v1",
        )
    assert existing_output.read_bytes() == b"preserve"

    with pytest.raises(DisasterRecoveryError, match="does not exist"):
        recovery.create_backup(
            data_dir=tmp_path / "missing-data",
            output_path=tmp_path / "new.ypbak",
            master_key=KEY_V1,
            key_id="key-v1",
        )
    with pytest.raises(DisasterRecoveryError, match="at least one"):
        recovery.prune_backups(backup_dir=tmp_path, keep=0, apply=False)
    with pytest.raises(DisasterRecoveryError, match="directory does not exist"):
        recovery.prune_backups(
            backup_dir=tmp_path / "missing-backups",
            keep=1,
            apply=False,
        )
    with pytest.raises(DisasterRecoveryError, match="broad system directory"):
        recovery.restore_backup(
            archive_path=tmp_path / "missing.ypbak",
            target_data_dir=Path.home(),
            master_key=KEY_V1,
            force=False,
        )


def test_authenticated_archive_still_rejects_unsafe_payloads(tmp_path) -> None:
    payload = tmp_path / "payload.zip"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("manifest.json", b"{}")
    forged = tmp_path / "forged.ypbak"
    _encrypt_payload(
        payload,
        forged,
        KEY_V1,
        key_id="key-v1",
        archive_id="00000000-0000-4000-8000-000000000001",
        created_at="2026-07-21T00:00:00+00:00",
    )
    with pytest.raises(DisasterRecoveryError, match="member set"):
        DisasterRecoveryService().verify_backup(
            archive_path=forged,
            master_key=KEY_V1,
        )

    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("manifest.json", b"{}")
        archive.writestr("agent.sqlite3", b"x")
        archive.writestr("checkpoints.sqlite3", b"y")
    invalid_manifest = tmp_path / "invalid-manifest.ypbak"
    _encrypt_payload(
        payload,
        invalid_manifest,
        KEY_V1,
        key_id="key-v1",
        archive_id="00000000-0000-4000-8000-000000000002",
        created_at="2026-07-21T00:00:00+00:00",
    )
    with pytest.raises(DisasterRecoveryError, match="unsupported backup manifest"):
        DisasterRecoveryService().verify_backup(
            archive_path=invalid_manifest,
            master_key=KEY_V1,
        )


def test_backup_rejects_orphan_checkpoint_snapshot_after_bounded_retries(tmp_path) -> None:
    source_dir = tmp_path / "source"
    service = _seed_service(source_dir)
    service.close()
    checkpoint_path = source_dir / "checkpoints.sqlite3"
    with sqlite3.connect(checkpoint_path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(checkpoints)")]
        row = list(connection.execute("SELECT * FROM checkpoints LIMIT 1").fetchone())
        row[columns.index("thread_id")] = "orphan-checkpoint-thread"
        placeholders = ",".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO checkpoints({','.join(columns)}) VALUES ({placeholders})",
            row,
        )

    output = tmp_path / "orphan.ypbak"
    with pytest.raises(DisasterRecoveryError, match="after 3 attempts"):
        DisasterRecoveryService().create_backup(
            data_dir=source_dir,
            output_path=output,
            master_key=KEY_V1,
            key_id="key-v1",
        )
    assert not output.exists()


def test_new_directory_restore_has_no_rollback_set(tmp_path) -> None:
    source_dir = tmp_path / "source"
    service = _seed_service(source_dir)
    service.close()
    recovery = DisasterRecoveryService()
    archive = tmp_path / "yunpai-no-rollback.ypbak"
    recovery.create_backup(
        data_dir=source_dir,
        output_path=archive,
        master_key=KEY_V1,
        key_id="key-v1",
    )
    target = tmp_path / "new-target"
    restored = recovery.restore_backup(
        archive_path=archive,
        target_data_dir=target,
        master_key=KEY_V1,
        force=False,
    )
    with pytest.raises(DisasterRecoveryError, match="no previous database"):
        recovery.rollback_restore(
            receipt_path=Path(restored["receipt"]),
            target_data_dir=target,
        )


def test_rollback_failure_preserves_restored_set_and_previous_rollback(
    tmp_path,
    monkeypatch,
) -> None:
    source_dir = tmp_path / "source"
    source_service = _seed_service(source_dir)
    source_service.close()
    target_dir = tmp_path / "target"
    target_service = AgentService(make_settings(target_dir))
    target_service.close()
    recovery = DisasterRecoveryService()
    archive = tmp_path / "yunpai-rollback-failure.ypbak"
    recovery.create_backup(
        data_dir=source_dir,
        output_path=archive,
        master_key=KEY_V1,
        key_id="key-v1",
    )
    restored = recovery.restore_backup(
        archive_path=archive,
        target_data_dir=target_dir,
        master_key=KEY_V1,
        force=True,
    )
    restored_hashes = {
        name: _sha256(target_dir / name)
        for name in ("agent.sqlite3", "checkpoints.sqlite3")
    }
    rollback_dir = Path(restored["rollback_directory"])
    previous_hashes = {
        name: _sha256(rollback_dir / name)
        for name in ("agent.sqlite3", "checkpoints.sqlite3")
    }
    real_replace = os.replace
    failed = False

    def fail_previous_checkpoint_install(source, target):
        nonlocal failed
        source_path = Path(source)
        target_path = Path(target)
        if (
            not failed
            and source_path == rollback_dir / "checkpoints.sqlite3"
            and target_path == target_dir / "checkpoints.sqlite3"
        ):
            failed = True
            raise OSError("injected rollback checkpoint failure")
        return real_replace(source, target)

    monkeypatch.setattr(
        disaster_recovery_module.os,
        "replace",
        fail_previous_checkpoint_install,
    )
    with pytest.raises(DisasterRecoveryError, match="restored database set was preserved"):
        recovery.rollback_restore(
            receipt_path=Path(restored["receipt"]),
            target_data_dir=target_dir,
        )
    assert failed is True
    assert {
        name: _sha256(target_dir / name)
        for name in ("agent.sqlite3", "checkpoints.sqlite3")
    } == restored_hashes
    assert {
        name: _sha256(rollback_dir / name)
        for name in ("agent.sqlite3", "checkpoints.sqlite3")
    } == previous_hashes
    assert not list(tmp_path.glob(".target-rollback-forward-*"))


def test_header_and_manifest_validation_rejects_each_trust_boundary() -> None:
    archive_id = "00000000-0000-4000-8000-000000000010"
    created_at = "2026-07-21T00:00:00+00:00"
    header = {
        "format": "yunpai.encrypted-backup",
        "format_version": 1,
        "algorithm": "AES-256-GCM",
        "kdf": "HKDF-SHA256",
        "key_id": "key-v1",
        "archive_id": archive_id,
        "created_at": created_at,
        "salt": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "nonce": "AAAAAAAAAAAAAAAA",
    }
    assert _validate_header(header)["key_id"] == "key-v1"
    header_mutations = [
        ("format", "other"),
        ("format_version", 2),
        ("algorithm", "none"),
        ("kdf", "none"),
        ("key_id", "bad key id"),
        ("archive_id", "not-a-uuid"),
        ("created_at", "no-timezone"),
        ("salt", "AA"),
        ("nonce", "AA"),
    ]
    for field, value in header_mutations:
        invalid = dict(header)
        invalid[field] = value
        with pytest.raises(DisasterRecoveryError):
            _validate_header(invalid)

    database_metadata = {
        "integrity_check": "ok",
        "user_version": 12,
        "counts": {},
    }
    manifest = {
        "format": "yunpai.sqlite.snapshot-set",
        "format_version": 1,
        "archive_id": archive_id,
        "created_at": created_at,
        "application_version": "0.11.0",
        "schema_version": Database.SCHEMA_VERSION,
        "capture": {},
        "files": [
            {
                "name": "agent.sqlite3",
                "bytes": 1,
                "sha256": "a" * 64,
                "database": database_metadata,
            },
            {
                "name": "checkpoints.sqlite3",
                "bytes": 1,
                "sha256": "b" * 64,
                "database": {
                    "integrity_check": "ok",
                    "user_version": 0,
                    "table_count": 0,
                    "thread_count": 0,
                },
            },
        ],
    }
    assert _validate_manifest(manifest, header)["archive_id"] == archive_id
    pre_v32_manifest = deepcopy(manifest)
    pre_v32_manifest["schema_version"] = 30
    with pytest.raises(
        DisasterRecoveryError,
        match="backup schema is not supported by this application",
    ):
        _validate_manifest(pre_v32_manifest, header)
    invalid_manifests = []
    for field, value in (
        ("format", "other"),
        ("format_version", 2),
        ("archive_id", "different"),
        ("created_at", "different"),
        ("schema_version", 999),
        ("files", "not-a-list"),
    ):
        invalid = deepcopy(manifest)
        invalid[field] = value
        invalid_manifests.append(invalid)
    duplicate_names = deepcopy(manifest)
    duplicate_names["files"][1]["name"] = "agent.sqlite3"
    invalid_manifests.append(duplicate_names)
    invalid_size = deepcopy(manifest)
    invalid_size["files"][0]["bytes"] = 0
    invalid_manifests.append(invalid_size)
    invalid_digest = deepcopy(manifest)
    invalid_digest["files"][0]["sha256"] = "invalid"
    invalid_manifests.append(invalid_digest)
    for invalid in invalid_manifests:
        with pytest.raises(DisasterRecoveryError):
            _validate_manifest(invalid, header)


def test_v36_manifest_rejects_pre_v36_backup_schema() -> None:
    """T7：v36 升级后，灾备 manifest 用 != 精确比对拒绝 v34 及更早备份。

    schema_version == Database.SCHEMA_VERSION（现为 36）通过；
    schema_version=34（v36 前）拒绝——本次迁移作废全部历史备份。
    """
    archive_id = "00000000-0000-4000-8000-000000000036"
    created_at = "2026-08-18T00:00:00+00:00"
    header = {
        "format": "yunpai.encrypted-backup",
        "format_version": 1,
        "algorithm": "AES-256-GCM",
        "kdf": "HKDF-SHA256",
        "key_id": "key-v1",
        "archive_id": archive_id,
        "created_at": created_at,
        "salt": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "nonce": "AAAAAAAAAAAAAAAA",
    }
    manifest = {
        "format": "yunpai.sqlite.snapshot-set",
        "format_version": 1,
        "archive_id": archive_id,
        "created_at": created_at,
        "application_version": "0.11.0",
        "schema_version": Database.SCHEMA_VERSION,
        "capture": {},
        "files": [
            {
                "name": "agent.sqlite3",
                "bytes": 1,
                "sha256": "a" * 64,
                "database": {"integrity_check": "ok", "user_version": 36, "counts": {}},
            },
            {
                "name": "checkpoints.sqlite3",
                "bytes": 1,
                "sha256": "b" * 64,
                "database": {"integrity_check": "ok", "user_version": 0},
            },
        ],
    }
    assert _validate_manifest(manifest, header)["archive_id"] == archive_id
    pre_v36_manifest = deepcopy(manifest)
    pre_v36_manifest["schema_version"] = 34
    with pytest.raises(
        DisasterRecoveryError,
        match="backup schema is not supported by this application",
    ):
        _validate_manifest(pre_v36_manifest, header)
