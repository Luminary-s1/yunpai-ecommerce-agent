from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .database import Database


def stable_rollout_bucket(salt: str, unit: str) -> int:
    """Deterministic 0-9999 bucket, same formula the release gate uses."""
    digest = hashlib.sha256(f"{salt}:{unit}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def in_rollout(salt: str, unit: str, traffic_percentage: int) -> bool:
    return stable_rollout_bucket(salt, unit) < int(traffic_percentage) * 100


def active_rollouts(
    db: Database, tenant_id: str, subject_type: str
) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM staged_rollouts
            WHERE tenant_id=? AND subject_type=? AND status='active'
            """,
            (tenant_id, subject_type),
        ).fetchall()
    return [dict(row) for row in rows]


def rollout_choice(
    rollout: Mapping[str, Any], unit: str | None
) -> str | None:
    """The candidate id the unit should see, or None to stay on the baseline."""
    if unit is None:
        return None
    if in_rollout(
        str(rollout["rollout_salt"]), unit, int(rollout["traffic_percentage"])
    ):
        return str(rollout["candidate_id"])
    return None
