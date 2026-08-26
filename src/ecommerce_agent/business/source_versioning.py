from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, Literal


WriteDecision = Literal["apply", "idempotent"]


class SourceVersionError(ValueError):
    """Raised when an external fact violates the immutable source-version contract."""


def canonical_source_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SourceVersionError("source_updated_at_timezone_required")
    return value.astimezone(UTC).isoformat()


def payload_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def decide_write(
    *,
    existing_source_time: str,
    existing_payload_hash: str,
    incoming_source_time: str,
    incoming_payload_hash: str,
    incoming_compatible_hashes: Collection[str] = (),
) -> WriteDecision:
    existing = datetime.fromisoformat(existing_source_time).astimezone(UTC)
    incoming = datetime.fromisoformat(incoming_source_time).astimezone(UTC)
    if incoming < existing:
        raise SourceVersionError("stale_source_version")
    if incoming == existing:
        if (
            incoming_payload_hash == existing_payload_hash
            or existing_payload_hash in incoming_compatible_hashes
        ):
            return "idempotent"
        raise SourceVersionError("source_version_conflict")
    return "apply"
