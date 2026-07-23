from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import struct
import tempfile
import uuid
import zipfile
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import __version__
from .database import Database


class DisasterRecoveryError(RuntimeError):
    pass


class BackupConsistencyError(DisasterRecoveryError):
    pass


MAGIC = b"YPAIBAK1"
FORMAT_VERSION = 1
PREFIX = struct.Struct(">8sBI")
TAG_BYTES = 16
MAX_HEADER_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DATABASE_BYTES = 64 * 1024 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
DATABASE_NAMES = ("agent.sqlite3", "checkpoints.sqlite3")
KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class DataDirectoryLock:
    def __init__(self, data_dir: Path):
        self.path = data_dir.resolve() / ".yunpai-runtime.lock"
        self._handle = None
        self._locked = False

    def acquire(self) -> None:
        if self._locked:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise DisasterRecoveryError(
                "data directory is in use; stop the service before restore or second startup"
            ) from exc
        self._handle = handle
        self._locked = True

    def release(self) -> None:
        if not self._locked or self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
            self._locked = False

    def __enter__(self) -> "DataDirectoryLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str, *, field: str) -> bytes:
    if not isinstance(value, str):
        raise DisasterRecoveryError(f"invalid backup header field: {field}")
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise DisasterRecoveryError(f"invalid backup header field: {field}") from exc


def decode_backup_key(value: str, *, variable: str = "BACKUP_ENCRYPTION_KEY") -> bytes:
    raw = value.strip()
    if not raw:
        raise DisasterRecoveryError(f"{variable} is required")
    try:
        if len(raw) == 64 and all(character in "0123456789abcdefABCDEF" for character in raw):
            decoded = bytes.fromhex(raw)
        else:
            decoded = _b64decode(raw, field=variable)
    except DisasterRecoveryError as exc:
        raise DisasterRecoveryError(
            f"{variable} must be URL-safe base64 or hexadecimal"
        ) from exc
    if len(decoded) != 32:
        raise DisasterRecoveryError(f"{variable} must decode to exactly 32 bytes")
    return decoded


def validate_key_id(value: str, *, variable: str = "BACKUP_KEY_ID") -> str:
    key_id = value.strip()
    if not KEY_ID_PATTERN.fullmatch(key_id):
        raise DisasterRecoveryError(
            f"{variable} must contain 1-64 letters, digits, dot, underscore, colon, or dash"
        )
    return key_id


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _derive_key(master_key: bytes, salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"yunpai-ecommerce-agent-backup-v1",
    ).derive(master_key)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _set_private_permissions(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _sqlite_uri(path: Path, *, immutable: bool = False) -> str:
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return f"{path.resolve().as_uri()}{suffix}"


def _sqlite_backup(source_path: Path, target_path: Path) -> None:
    if not source_path.is_file():
        raise DisasterRecoveryError(f"database does not exist: {source_path.name}")
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(_sqlite_uri(source_path), uri=True, timeout=20)
        source.execute("PRAGMA query_only = ON")
        target = sqlite3.connect(target_path, timeout=20)
        source.backup(target, pages=512, sleep=0.01)
        target.commit()
        journal_mode = str(target.execute("PRAGMA journal_mode = DELETE").fetchone()[0])
        if journal_mode.lower() != "delete":
            raise DisasterRecoveryError(
                f"failed to normalize snapshot journal mode for {source_path.name}"
            )
        target.execute("PRAGMA synchronous = FULL")
        target.commit()
    except sqlite3.Error as exc:
        raise DisasterRecoveryError(
            f"failed to snapshot {source_path.name}: {type(exc).__name__}"
        ) from exc
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
    _set_private_permissions(target_path)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _database_integrity(connection: sqlite3.Connection) -> str:
    results = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if results != ["ok"]:
        raise DisasterRecoveryError("SQLite integrity_check failed")
    return "ok"


def _inspect_app_database(path: Path) -> dict[str, Any]:
    try:
        with closing(
            sqlite3.connect(_sqlite_uri(path, immutable=True), uri=True, timeout=20)
        ) as connection:
            integrity = _database_integrity(connection)
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version != Database.SCHEMA_VERSION:
                raise DisasterRecoveryError(
                    f"backup requires schema {Database.SCHEMA_VERSION}, found {version}"
                )
            Database._validate_schema(connection)
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "sessions",
                    "knowledge",
                    "sop_definitions",
                    "channel_outbox",
                    "channel_events",
                )
            }
    except DisasterRecoveryError:
        raise
    except (sqlite3.Error, RuntimeError) as exc:
        raise DisasterRecoveryError("agent database validation failed") from exc
    return {"integrity_check": integrity, "user_version": version, "counts": counts}


def _checkpoint_thread_ids(connection: sqlite3.Connection) -> set[str]:
    result: set[str] = set()
    for table in ("checkpoints", "writes", "checkpoint_blobs"):
        if not _table_exists(connection, table):
            continue
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if "thread_id" not in columns:
            continue
        result.update(
            str(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT thread_id FROM {table} WHERE thread_id IS NOT NULL"
            )
        )
    return result


def _inspect_checkpoint_database(path: Path) -> dict[str, Any]:
    try:
        with closing(
            sqlite3.connect(_sqlite_uri(path, immutable=True), uri=True, timeout=20)
        ) as connection:
            integrity = _database_integrity(connection)
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = sorted(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            )
            thread_count = len(_checkpoint_thread_ids(connection))
    except sqlite3.Error as exc:
        raise DisasterRecoveryError("checkpoint database validation failed") from exc
    return {
        "integrity_check": integrity,
        "user_version": version,
        "table_count": len(tables),
        "thread_count": thread_count,
    }


def _cross_database_consistency(app_path: Path, checkpoint_path: Path) -> dict[str, int]:
    try:
        with closing(
            sqlite3.connect(_sqlite_uri(app_path, immutable=True), uri=True, timeout=20)
        ) as app_connection:
            session_ids = {
                str(row[0]) for row in app_connection.execute("SELECT id FROM sessions")
            }
        with closing(
            sqlite3.connect(
                _sqlite_uri(checkpoint_path, immutable=True), uri=True, timeout=20
            )
        ) as checkpoint_connection:
            checkpoint_ids = _checkpoint_thread_ids(checkpoint_connection)
    except sqlite3.Error as exc:
        raise BackupConsistencyError("cross-database consistency query failed") from exc
    orphaned = checkpoint_ids - session_ids
    if orphaned:
        raise BackupConsistencyError(
            "checkpoint snapshot contains threads absent from the session snapshot"
        )
    return {
        "session_count": len(session_ids),
        "checkpoint_thread_count": len(checkpoint_ids),
        "orphan_checkpoint_threads": 0,
    }


def _snapshot_pair(data_dir: Path, staging_dir: Path) -> tuple[Path, Path, dict[str, Any]]:
    app_source = data_dir / DATABASE_NAMES[0]
    checkpoint_source = data_dir / DATABASE_NAMES[1]
    app_snapshot = staging_dir / DATABASE_NAMES[0]
    checkpoint_snapshot = staging_dir / DATABASE_NAMES[1]

    # Capturing checkpoints first guarantees that any new session appearing between
    # the two snapshots exists in the later application snapshot.
    _sqlite_backup(checkpoint_source, checkpoint_snapshot)
    _sqlite_backup(app_source, app_snapshot)
    app_inspection = _inspect_app_database(app_snapshot)
    checkpoint_inspection = _inspect_checkpoint_database(checkpoint_snapshot)
    consistency = _cross_database_consistency(app_snapshot, checkpoint_snapshot)
    return app_snapshot, checkpoint_snapshot, {
        "agent.sqlite3": app_inspection,
        "checkpoints.sqlite3": checkpoint_inspection,
        "cross_database": consistency,
    }


def _build_manifest(
    *,
    archive_id: str,
    created_at: str,
    app_snapshot: Path,
    checkpoint_snapshot: Path,
    inspection: Mapping[str, Any],
    capture_mode: str,
) -> dict[str, Any]:
    files = []
    for path in (app_snapshot, checkpoint_snapshot):
        files.append(
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "database": inspection[path.name],
            }
        )
    return {
        "format": "yunpai.sqlite.snapshot-set",
        "format_version": FORMAT_VERSION,
        "archive_id": archive_id,
        "created_at": created_at,
        "application_version": __version__,
        "schema_version": Database.SCHEMA_VERSION,
        "capture": {
            "method": "sqlite_online_backup",
            "mode": capture_mode,
            "order": ["checkpoints.sqlite3", "agent.sqlite3"],
            "cross_database_invariant": "checkpoint_thread_is_known_session",
            **inspection["cross_database"],
        },
        "files": files,
    }


def _write_payload(
    path: Path,
    manifest: Mapping[str, Any],
    app_snapshot: Path,
    checkpoint_snapshot: Path,
) -> None:
    with zipfile.ZipFile(
        path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        archive.writestr("manifest.json", _canonical_json(manifest))
        archive.write(app_snapshot, arcname=app_snapshot.name)
        archive.write(checkpoint_snapshot, arcname=checkpoint_snapshot.name)
    _set_private_permissions(path)


def _build_header(*, archive_id: str, created_at: str, key_id: str) -> dict[str, Any]:
    return {
        "format": "yunpai.encrypted-backup",
        "format_version": FORMAT_VERSION,
        "algorithm": "AES-256-GCM",
        "kdf": "HKDF-SHA256",
        "key_id": validate_key_id(key_id),
        "archive_id": archive_id,
        "created_at": created_at,
        "salt": _b64encode(os.urandom(32)),
        "nonce": _b64encode(os.urandom(12)),
    }


def _validate_header(header: Any) -> dict[str, Any]:
    if not isinstance(header, dict):
        raise DisasterRecoveryError("invalid backup header")
    expected = {
        "format": "yunpai.encrypted-backup",
        "format_version": FORMAT_VERSION,
        "algorithm": "AES-256-GCM",
        "kdf": "HKDF-SHA256",
    }
    for field, value in expected.items():
        if header.get(field) != value:
            raise DisasterRecoveryError(f"unsupported backup header: {field}")
    key_id = validate_key_id(str(header.get("key_id", "")), variable="archive key_id")
    archive_id = str(header.get("archive_id", ""))
    try:
        uuid.UUID(archive_id)
    except ValueError as exc:
        raise DisasterRecoveryError("invalid backup archive_id") from exc
    created_at = header.get("created_at")
    if not isinstance(created_at, str) or len(created_at) > 64:
        raise DisasterRecoveryError("invalid backup created_at")
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise DisasterRecoveryError("invalid backup created_at") from exc
    if parsed_created_at.tzinfo is None:
        raise DisasterRecoveryError("backup created_at must include a timezone")
    salt = _b64decode(header.get("salt"), field="salt")
    nonce = _b64decode(header.get("nonce"), field="nonce")
    if len(salt) != 32 or len(nonce) != 12:
        raise DisasterRecoveryError("invalid backup cryptographic parameters")
    return {**header, "key_id": key_id, "salt_bytes": salt, "nonce_bytes": nonce}


def read_backup_header(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            prefix = source.read(PREFIX.size)
            if len(prefix) != PREFIX.size:
                raise DisasterRecoveryError("backup file is truncated")
            magic, version, header_length = PREFIX.unpack(prefix)
            if magic != MAGIC or version != FORMAT_VERSION:
                raise DisasterRecoveryError("unsupported backup format")
            if not 1 <= header_length <= MAX_HEADER_BYTES:
                raise DisasterRecoveryError("invalid backup header length")
            raw_header = source.read(header_length)
            if len(raw_header) != header_length:
                raise DisasterRecoveryError("backup header is truncated")
            try:
                header = json.loads(raw_header.decode("ascii"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DisasterRecoveryError("backup header is invalid") from exc
    except OSError as exc:
        raise DisasterRecoveryError("cannot read backup archive") from exc
    validated = _validate_header(header)
    return {
        key: value
        for key, value in validated.items()
        if key not in {"salt_bytes", "nonce_bytes"}
    }


def _encrypt_payload(
    payload_path: Path,
    output_path: Path,
    master_key: bytes,
    *,
    key_id: str,
    archive_id: str,
    created_at: str,
) -> dict[str, Any]:
    if output_path.exists():
        raise DisasterRecoveryError("backup output already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    header = _build_header(
        archive_id=archive_id,
        created_at=created_at,
        key_id=key_id,
    )
    header_bytes = _canonical_json(header)
    prefix = PREFIX.pack(MAGIC, FORMAT_VERSION, len(header_bytes))
    salt = _b64decode(header["salt"], field="salt")
    nonce = _b64decode(header["nonce"], field="nonce")
    encryptor = Cipher(algorithms.AES(_derive_key(master_key, salt)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(prefix + header_bytes)
    partial = output_path.with_name(f".{output_path.name}.partial-{uuid.uuid4().hex}")
    try:
        with payload_path.open("rb") as source, partial.open("xb") as target:
            target.write(prefix)
            target.write(header_bytes)
            while chunk := source.read(CHUNK_BYTES):
                target.write(encryptor.update(chunk))
            target.write(encryptor.finalize())
            target.write(encryptor.tag)
            target.flush()
            os.fsync(target.fileno())
        _set_private_permissions(partial)
        if output_path.exists():
            raise DisasterRecoveryError("backup output was created concurrently")
        os.replace(partial, output_path)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return {
        "archive_id": archive_id,
        "created_at": created_at,
        "key_id": header["key_id"],
        "path": str(output_path.resolve()),
        "bytes": output_path.stat().st_size,
        "sha256": _sha256(output_path),
    }


def _decrypt_payload(archive_path: Path, output_path: Path, master_key: bytes) -> dict[str, Any]:
    try:
        total_bytes = archive_path.stat().st_size
        with archive_path.open("rb") as source:
            prefix = source.read(PREFIX.size)
            if len(prefix) != PREFIX.size:
                raise DisasterRecoveryError("backup file is truncated")
            magic, version, header_length = PREFIX.unpack(prefix)
            if magic != MAGIC or version != FORMAT_VERSION:
                raise DisasterRecoveryError("unsupported backup format")
            if not 1 <= header_length <= MAX_HEADER_BYTES:
                raise DisasterRecoveryError("invalid backup header length")
            header_bytes = source.read(header_length)
            if len(header_bytes) != header_length:
                raise DisasterRecoveryError("backup header is truncated")
            try:
                header = _validate_header(json.loads(header_bytes.decode("ascii")))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DisasterRecoveryError("backup header is invalid") from exc
            ciphertext_bytes = total_bytes - PREFIX.size - header_length - TAG_BYTES
            if ciphertext_bytes <= 0:
                raise DisasterRecoveryError("backup payload is truncated")
            source.seek(total_bytes - TAG_BYTES)
            tag = source.read(TAG_BYTES)
            source.seek(PREFIX.size + header_length)
            decryptor = Cipher(
                algorithms.AES(_derive_key(master_key, header["salt_bytes"])),
                modes.GCM(header["nonce_bytes"], tag),
            ).decryptor()
            decryptor.authenticate_additional_data(prefix + header_bytes)
            remaining = ciphertext_bytes
            with output_path.open("xb") as target:
                while remaining:
                    chunk = source.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        raise DisasterRecoveryError("backup payload is truncated")
                    target.write(decryptor.update(chunk))
                    remaining -= len(chunk)
                try:
                    target.write(decryptor.finalize())
                except InvalidTag as exc:
                    raise DisasterRecoveryError(
                        "backup authentication failed; key is wrong or archive was modified"
                    ) from exc
                target.flush()
                os.fsync(target.fileno())
    except DisasterRecoveryError:
        output_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        output_path.unlink(missing_ok=True)
        raise DisasterRecoveryError("cannot decrypt backup archive") from exc
    _set_private_permissions(output_path)
    return {
        key: value
        for key, value in header.items()
        if key not in {"salt_bytes", "nonce_bytes"}
    }


def _validate_manifest(value: Any, header: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DisasterRecoveryError("backup manifest is invalid")
    if value.get("format") != "yunpai.sqlite.snapshot-set":
        raise DisasterRecoveryError("unsupported backup manifest")
    if value.get("format_version") != FORMAT_VERSION:
        raise DisasterRecoveryError("unsupported backup manifest version")
    if value.get("archive_id") != header["archive_id"]:
        raise DisasterRecoveryError("backup header and manifest archive_id differ")
    if value.get("created_at") != header["created_at"]:
        raise DisasterRecoveryError("backup header and manifest timestamp differ")
    if value.get("schema_version") != Database.SCHEMA_VERSION:
        raise DisasterRecoveryError("backup schema is not supported by this application")
    files = value.get("files")
    if not isinstance(files, list) or len(files) != len(DATABASE_NAMES):
        raise DisasterRecoveryError("backup manifest file list is invalid")
    names = [item.get("name") for item in files if isinstance(item, dict)]
    if set(names) != set(DATABASE_NAMES) or len(names) != len(DATABASE_NAMES):
        raise DisasterRecoveryError("backup manifest database set is invalid")
    for item in files:
        size = item.get("bytes")
        digest = item.get("sha256")
        if not isinstance(size, int) or not 0 < size <= MAX_DATABASE_BYTES:
            raise DisasterRecoveryError("backup manifest database size is invalid")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DisasterRecoveryError("backup manifest database digest is invalid")
    return value


def _extract_and_validate_payload(
    payload_path: Path,
    staging_dir: Path,
    header: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_members = {"manifest.json", *DATABASE_NAMES}
    try:
        with zipfile.ZipFile(payload_path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or set(names) != expected_members:
                raise DisasterRecoveryError("backup payload member set is invalid")
            if any(
                info.is_dir()
                or Path(info.filename).name != info.filename
                or info.file_size < 0
                for info in infos
            ):
                raise DisasterRecoveryError("backup payload contains an unsafe member")
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > MAX_MANIFEST_BYTES:
                raise DisasterRecoveryError("backup manifest is too large")
            try:
                manifest = _validate_manifest(
                    json.loads(archive.read(manifest_info).decode("ascii")),
                    header,
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DisasterRecoveryError("backup manifest is invalid") from exc
            declared = {item["name"]: item for item in manifest["files"]}
            for name in DATABASE_NAMES:
                info = archive.getinfo(name)
                expected_size = declared[name]["bytes"]
                if info.file_size != expected_size:
                    raise DisasterRecoveryError("backup payload size differs from manifest")
                destination = staging_dir / name
                digest = hashlib.sha256()
                written = 0
                with archive.open(info, "r") as source, destination.open("xb") as target:
                    while chunk := source.read(CHUNK_BYTES):
                        written += len(chunk)
                        if written > expected_size:
                            raise DisasterRecoveryError("backup member exceeds declared size")
                        digest.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                _set_private_permissions(destination)
                if written != expected_size or not secrets.compare_digest(
                    digest.hexdigest(), declared[name]["sha256"]
                ):
                    raise DisasterRecoveryError("backup member digest validation failed")
    except zipfile.BadZipFile as exc:
        raise DisasterRecoveryError("backup payload is not a valid archive") from exc

    actual_inspection = {
        "agent.sqlite3": _inspect_app_database(staging_dir / "agent.sqlite3"),
        "checkpoints.sqlite3": _inspect_checkpoint_database(
            staging_dir / "checkpoints.sqlite3"
        ),
    }
    consistency = _cross_database_consistency(
        staging_dir / "agent.sqlite3",
        staging_dir / "checkpoints.sqlite3",
    )
    for item in manifest["files"]:
        if item.get("database") != actual_inspection[item["name"]]:
            raise DisasterRecoveryError("database inspection differs from backup manifest")
    capture = manifest.get("capture")
    if not isinstance(capture, dict):
        raise DisasterRecoveryError("backup capture metadata is invalid")
    for field, value in consistency.items():
        if capture.get(field) != value:
            raise DisasterRecoveryError("cross-database consistency differs from manifest")
    return manifest, {**actual_inspection, "cross_database": consistency}


@contextmanager
def _materialized_archive(
    archive_path: Path,
    master_key: bytes,
    *,
    parent: Path | None = None,
) -> Iterator[tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any]]]:
    if not archive_path.is_file():
        raise DisasterRecoveryError("backup archive does not exist")
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".yunpai-verify-",
        dir=parent,
        ignore_cleanup_errors=True,
    ) as temporary:
        staging_dir = Path(temporary)
        payload_path = staging_dir / "payload.zip"
        header = _decrypt_payload(archive_path, payload_path, master_key)
        manifest, inspection = _extract_and_validate_payload(
            payload_path,
            staging_dir,
            header,
        )
        yield staging_dir, header, manifest, inspection


def _safe_target_directory(path: Path) -> Path:
    target = path.resolve()
    if target == Path(target.anchor) or target == Path.home().resolve():
        raise DisasterRecoveryError("refusing to restore into a broad system directory")
    return target


def _active_sidecars(target_dir: Path) -> list[str]:
    result = []
    for database_name in DATABASE_NAMES:
        for suffix in ("-wal", "-shm", "-journal"):
            candidate = target_dir / f"{database_name}{suffix}"
            if candidate.exists():
                result.append(candidate.name)
    return result


def _copy_for_install(source: Path, target: Path) -> None:
    with source.open("rb") as input_handle, target.open("xb") as output_handle:
        shutil.copyfileobj(input_handle, output_handle, length=CHUNK_BYTES)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    _set_private_permissions(target)


def _read_receipt(path: Path, *, expected_format: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DisasterRecoveryError("restore receipt cannot be read") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise DisasterRecoveryError("restore receipt is too large")
    try:
        receipt = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DisasterRecoveryError("restore receipt is invalid") from exc
    if not isinstance(receipt, dict) or receipt.get("format") != expected_format:
        raise DisasterRecoveryError("restore receipt format is invalid")
    if receipt.get("format_version") != FORMAT_VERSION:
        raise DisasterRecoveryError("restore receipt version is unsupported")
    archive_id = str(receipt.get("archive_id", ""))
    try:
        uuid.UUID(archive_id)
    except ValueError as exc:
        raise DisasterRecoveryError("restore receipt archive_id is invalid") from exc
    return receipt


class DisasterRecoveryService:
    def create_backup(
        self,
        *,
        data_dir: Path,
        output_path: Path,
        master_key: bytes,
        key_id: str,
        require_stopped: bool = False,
    ) -> dict[str, Any]:
        data_dir = data_dir.resolve()
        output_path = output_path.resolve()
        if require_stopped:
            with DataDirectoryLock(data_dir):
                return self._create_backup_unlocked(
                    data_dir=data_dir,
                    output_path=output_path,
                    master_key=master_key,
                    key_id=key_id,
                    capture_mode="offline_runtime_locked",
                )
        return self._create_backup_unlocked(
            data_dir=data_dir,
            output_path=output_path,
            master_key=master_key,
            key_id=key_id,
            capture_mode="online_identity_consistent",
        )

    def _create_backup_unlocked(
        self,
        *,
        data_dir: Path,
        output_path: Path,
        master_key: bytes,
        key_id: str,
        capture_mode: str,
    ) -> dict[str, Any]:
        if output_path.exists():
            raise DisasterRecoveryError("backup output already exists")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        archive_id = str(uuid.uuid4())
        created_at = _utc_now()
        with tempfile.TemporaryDirectory(
            prefix=".yunpai-backup-",
            dir=output_path.parent,
            ignore_cleanup_errors=True,
        ) as temporary:
            staging_dir = Path(temporary)
            last_consistency_error: BackupConsistencyError | None = None
            for _ in range(3):
                for name in DATABASE_NAMES:
                    (staging_dir / name).unlink(missing_ok=True)
                try:
                    app_snapshot, checkpoint_snapshot, inspection = _snapshot_pair(
                        data_dir,
                        staging_dir,
                    )
                    break
                except BackupConsistencyError as exc:
                    last_consistency_error = exc
            else:
                raise DisasterRecoveryError(
                    "could not obtain a cross-database-consistent snapshot after 3 attempts"
                ) from last_consistency_error
            manifest = _build_manifest(
                archive_id=archive_id,
                created_at=created_at,
                app_snapshot=app_snapshot,
                checkpoint_snapshot=checkpoint_snapshot,
                inspection=inspection,
                capture_mode=capture_mode,
            )
            payload_path = staging_dir / "payload.zip"
            _write_payload(payload_path, manifest, app_snapshot, checkpoint_snapshot)
            result = _encrypt_payload(
                payload_path,
                output_path,
                master_key,
                key_id=key_id,
                archive_id=archive_id,
                created_at=created_at,
            )
        return {
            "ok": True,
            **result,
            "schema_version": Database.SCHEMA_VERSION,
            "capture": manifest["capture"],
            "databases": {
                item["name"]: {
                    "bytes": item["bytes"],
                    "sha256": item["sha256"],
                    "database": item["database"],
                }
                for item in manifest["files"]
            },
        }

    def verify_backup(
        self,
        *,
        archive_path: Path,
        master_key: bytes,
    ) -> dict[str, Any]:
        with _materialized_archive(archive_path.resolve(), master_key) as (
            _staging,
            header,
            manifest,
            inspection,
        ):
            return {
                "ok": True,
                "path": str(archive_path.resolve()),
                "archive_sha256": _sha256(archive_path.resolve()),
                "archive_id": header["archive_id"],
                "created_at": header["created_at"],
                "key_id": header["key_id"],
                "application_version": manifest["application_version"],
                "schema_version": manifest["schema_version"],
                "capture": manifest["capture"],
                "databases": inspection,
            }

    def restore_backup(
        self,
        *,
        archive_path: Path,
        target_data_dir: Path,
        master_key: bytes,
        force: bool,
    ) -> dict[str, Any]:
        target_dir = _safe_target_directory(target_data_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        with DataDirectoryLock(target_dir):
            return self._restore_while_locked(
                archive_path=archive_path,
                target_dir=target_dir,
                master_key=master_key,
                force=force,
            )

    def _restore_while_locked(
        self,
        *,
        archive_path: Path,
        target_dir: Path,
        master_key: bytes,
        force: bool,
    ) -> dict[str, Any]:
        sidecars = _active_sidecars(target_dir)
        if sidecars:
            raise DisasterRecoveryError(
                "restore target has SQLite sidecar files; stop the service cleanly first: "
                + ", ".join(sidecars)
            )
        existing = [name for name in DATABASE_NAMES if (target_dir / name).exists()]
        if existing and not force:
            raise DisasterRecoveryError(
                "restore target already contains databases; use --force after stopping the service"
            )

        with _materialized_archive(
            archive_path.resolve(),
            master_key,
            parent=target_dir.parent,
        ) as (staging_dir, header, manifest, _inspection):
            archive_id = header["archive_id"]
            rollback_dir = target_dir.parent / (
                f".{target_dir.name}-restore-rollback-{archive_id}"
            )
            rollback_dir.mkdir(mode=0o700, exist_ok=False)
            moved: list[str] = []
            installed: list[str] = []
            temporary_targets: list[Path] = []
            receipt_path = target_dir / f"restore-receipt-{archive_id}.json"
            try:
                for name in DATABASE_NAMES:
                    current = target_dir / name
                    if current.exists():
                        os.replace(current, rollback_dir / name)
                        moved.append(name)
                for name in DATABASE_NAMES:
                    temporary_target = target_dir / f".{name}.restore-{uuid.uuid4().hex}"
                    temporary_targets.append(temporary_target)
                    _copy_for_install(staging_dir / name, temporary_target)
                    os.replace(temporary_target, target_dir / name)
                    installed.append(name)

                declared_files = {item["name"]: item for item in manifest["files"]}
                for name in DATABASE_NAMES:
                    if not secrets.compare_digest(
                        _sha256(target_dir / name),
                        declared_files[name]["sha256"],
                    ):
                        raise DisasterRecoveryError("restored database digest differs from manifest")

                restored_inspection = {
                    "agent.sqlite3": _inspect_app_database(target_dir / "agent.sqlite3"),
                    "checkpoints.sqlite3": _inspect_checkpoint_database(
                        target_dir / "checkpoints.sqlite3"
                    ),
                }
                restored_consistency = _cross_database_consistency(
                    target_dir / "agent.sqlite3",
                    target_dir / "checkpoints.sqlite3",
                )
                for name in DATABASE_NAMES:
                    if restored_inspection[name] != declared_files[name]["database"]:
                        raise DisasterRecoveryError(
                            "restored database inspection differs from manifest"
                        )
                receipt = {
                    "format": "yunpai.restore-receipt",
                    "format_version": FORMAT_VERSION,
                    "archive_id": archive_id,
                    "archive_sha256": _sha256(archive_path.resolve()),
                    "restored_at": _utc_now(),
                    "schema_version": manifest["schema_version"],
                    "rollback_directory": str(rollback_dir) if moved else None,
                }
                receipt_temporary = receipt_path.with_name(
                    f".{receipt_path.name}.partial-{uuid.uuid4().hex}"
                )
                temporary_targets.append(receipt_temporary)
                with receipt_temporary.open("xb") as receipt_handle:
                    receipt_handle.write(_canonical_json(receipt))
                    receipt_handle.flush()
                    os.fsync(receipt_handle.fileno())
                _set_private_permissions(receipt_temporary)
                os.replace(receipt_temporary, receipt_path)
            except Exception as exc:
                receipt_path.unlink(missing_ok=True)
                for temporary_target in temporary_targets:
                    temporary_target.unlink(missing_ok=True)
                for name in installed:
                    (target_dir / name).unlink(missing_ok=True)
                rollback_errors = []
                for name in moved:
                    try:
                        os.replace(rollback_dir / name, target_dir / name)
                    except OSError as rollback_exc:
                        rollback_errors.append(f"{name}:{type(rollback_exc).__name__}")
                if not rollback_errors:
                    try:
                        rollback_dir.rmdir()
                    except OSError:
                        pass
                if rollback_errors:
                    raise DisasterRecoveryError(
                        "restore failed and automatic rollback was incomplete; "
                        f"manual files remain in {rollback_dir}: {', '.join(rollback_errors)}"
                    ) from exc
                if isinstance(exc, DisasterRecoveryError):
                    raise
                raise DisasterRecoveryError(
                    f"restore failed and original databases were rolled back: {type(exc).__name__}"
                ) from exc
            finally:
                for temporary_target in temporary_targets:
                    temporary_target.unlink(missing_ok=True)

            if not moved:
                rollback_dir.rmdir()
                rollback_value = None
            else:
                rollback_value = str(rollback_dir)
            return {
                "ok": True,
                "archive_id": archive_id,
                "target_data_dir": str(target_dir),
                "receipt": str(receipt_path),
                "rollback_directory": rollback_value,
                "schema_version": manifest["schema_version"],
                "databases": {
                    **restored_inspection,
                    "cross_database": restored_consistency,
                },
            }

    def rekey_backup(
        self,
        *,
        archive_path: Path,
        output_path: Path,
        old_master_key: bytes,
        new_master_key: bytes,
        new_key_id: str,
    ) -> dict[str, Any]:
        output_path = output_path.resolve()
        with _materialized_archive(archive_path.resolve(), old_master_key) as (
            staging_dir,
            header,
            manifest,
            _inspection,
        ):
            payload_path = staging_dir / "payload.zip"
            result = _encrypt_payload(
                payload_path,
                output_path,
                new_master_key,
                key_id=new_key_id,
                archive_id=header["archive_id"],
                created_at=header["created_at"],
            )
        verified = self.verify_backup(
            archive_path=output_path,
            master_key=new_master_key,
        )
        return {
            "ok": True,
            "source": str(archive_path.resolve()),
            "source_key_id": header["key_id"],
            "output": result["path"],
            "new_key_id": verified["key_id"],
            "archive_id": manifest["archive_id"],
            "sha256": result["sha256"],
        }

    def rollback_restore(
        self,
        *,
        receipt_path: Path,
        target_data_dir: Path,
    ) -> dict[str, Any]:
        target_dir = _safe_target_directory(target_data_dir)
        receipt_path = receipt_path.resolve()
        if receipt_path.parent != target_dir:
            raise DisasterRecoveryError("restore receipt must be inside the target data directory")
        receipt = _read_receipt(receipt_path, expected_format="yunpai.restore-receipt")
        archive_id = receipt["archive_id"]
        expected_rollback_dir = target_dir.parent / (
            f".{target_dir.name}-restore-rollback-{archive_id}"
        )
        declared_rollback = receipt.get("rollback_directory")
        if declared_rollback is None:
            raise DisasterRecoveryError("restore has no previous database set to roll back")
        if Path(str(declared_rollback)).resolve() != expected_rollback_dir.resolve():
            raise DisasterRecoveryError("restore receipt rollback directory is invalid")

        with DataDirectoryLock(target_dir):
            sidecars = _active_sidecars(target_dir)
            if sidecars:
                raise DisasterRecoveryError(
                    "rollback target has SQLite sidecar files; stop the service cleanly first: "
                    + ", ".join(sidecars)
                )
            for name in DATABASE_NAMES:
                if not (target_dir / name).is_file():
                    raise DisasterRecoveryError(f"current database is missing: {name}")
                if not (expected_rollback_dir / name).is_file():
                    raise DisasterRecoveryError(f"rollback database is missing: {name}")
            _inspect_app_database(expected_rollback_dir / "agent.sqlite3")
            _inspect_checkpoint_database(expected_rollback_dir / "checkpoints.sqlite3")
            _cross_database_consistency(
                expected_rollback_dir / "agent.sqlite3",
                expected_rollback_dir / "checkpoints.sqlite3",
            )

            forward_dir = target_dir.parent / (
                f".{target_dir.name}-rollback-forward-{archive_id}"
            )
            forward_dir.mkdir(mode=0o700, exist_ok=False)
            current_moved: list[str] = []
            previous_installed: list[str] = []
            rollback_receipt_path = target_dir / f"rollback-receipt-{archive_id}.json"
            try:
                for name in DATABASE_NAMES:
                    os.replace(target_dir / name, forward_dir / name)
                    current_moved.append(name)
                for name in DATABASE_NAMES:
                    os.replace(expected_rollback_dir / name, target_dir / name)
                    previous_installed.append(name)
                inspection = {
                    "agent.sqlite3": _inspect_app_database(target_dir / "agent.sqlite3"),
                    "checkpoints.sqlite3": _inspect_checkpoint_database(
                        target_dir / "checkpoints.sqlite3"
                    ),
                }
                consistency = _cross_database_consistency(
                    target_dir / "agent.sqlite3",
                    target_dir / "checkpoints.sqlite3",
                )
                rollback_receipt = {
                    "format": "yunpai.rollback-receipt",
                    "format_version": FORMAT_VERSION,
                    "archive_id": archive_id,
                    "rolled_back_at": _utc_now(),
                    "source_restore_receipt": receipt_path.name,
                    "forward_directory": str(forward_dir),
                }
                partial_receipt = rollback_receipt_path.with_name(
                    f".{rollback_receipt_path.name}.partial-{uuid.uuid4().hex}"
                )
                with partial_receipt.open("xb") as handle:
                    handle.write(_canonical_json(rollback_receipt))
                    handle.flush()
                    os.fsync(handle.fileno())
                _set_private_permissions(partial_receipt)
                os.replace(partial_receipt, rollback_receipt_path)
            except Exception as exc:
                rollback_receipt_path.unlink(missing_ok=True)
                for name in previous_installed:
                    try:
                        os.replace(target_dir / name, expected_rollback_dir / name)
                    except OSError:
                        pass
                recovery_errors = []
                for name in current_moved:
                    try:
                        os.replace(forward_dir / name, target_dir / name)
                    except OSError as recovery_exc:
                        recovery_errors.append(f"{name}:{type(recovery_exc).__name__}")
                if not recovery_errors:
                    try:
                        forward_dir.rmdir()
                    except OSError:
                        pass
                if recovery_errors:
                    raise DisasterRecoveryError(
                        "rollback failed and current database recovery was incomplete; "
                        f"manual files remain in {forward_dir}: {', '.join(recovery_errors)}"
                    ) from exc
                if isinstance(exc, DisasterRecoveryError):
                    raise
                raise DisasterRecoveryError(
                    f"rollback failed; restored database set was preserved: {type(exc).__name__}"
                ) from exc

            expected_rollback_dir.rmdir()
            return {
                "ok": True,
                "archive_id": archive_id,
                "target_data_dir": str(target_dir),
                "rollback_receipt": str(rollback_receipt_path),
                "forward_directory": str(forward_dir),
                "databases": {**inspection, "cross_database": consistency},
            }

    def prune_backups(
        self,
        *,
        backup_dir: Path,
        keep: int,
        apply: bool,
    ) -> dict[str, Any]:
        if keep < 1:
            raise DisasterRecoveryError("backup retention must keep at least one archive")
        directory = backup_dir.resolve()
        if not directory.is_dir():
            raise DisasterRecoveryError("backup directory does not exist")
        valid = []
        invalid = []
        for path in directory.glob("yunpai-*.ypbak"):
            try:
                header = read_backup_header(path)
            except DisasterRecoveryError:
                invalid.append(path.name)
                continue
            valid.append((header["created_at"], path.name, path))
        valid.sort(reverse=True)
        candidates = [item[2] for item in valid[keep:]]
        removed = []
        if apply:
            for path in candidates:
                path.unlink()
                removed.append(path.name)
        return {
            "ok": True,
            "dry_run": not apply,
            "directory": str(directory),
            "keep": keep,
            "valid_archives": len(valid),
            "invalid_archives_ignored": sorted(invalid),
            "candidates": [path.name for path in candidates],
            "removed": removed,
        }
