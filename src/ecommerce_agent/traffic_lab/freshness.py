from __future__ import annotations

from typing import Any

from ..business.source_versioning import payload_digest
from ..database import Database
from ..evidence_freshness import evidence_freshness
from ..traffic_source_identity import LEGACY_UNSCOPED_CONNECTOR_ID


def analysis_input_freshness(
    db: Database,
    tenant_id: str,
    experiment_id: str,
    evidence: dict[str, Any],
    *,
    analysis_run_id: str | None = None,
) -> dict[str, Any]:
    snapshot = evidence.get("input_snapshot")
    evidence_ref: dict[str, Any] = {"analysis_run_id": analysis_run_id}
    if not isinstance(snapshot, dict):
        return evidence_freshness(
            status="stale",
            reason_codes=["analysis_input_snapshot_invalid"],
            evidence_ref=evidence_ref,
            current_ref={"experiment_id": experiment_id},
        )
    experiment_snapshot = snapshot.get("experiment")
    revision_snapshots = snapshot.get("revisions")
    window_snapshots = snapshot.get("windows")
    bucket_snapshots = snapshot.get("buckets")
    if not (
        isinstance(experiment_snapshot, dict)
        and isinstance(revision_snapshots, list)
        and isinstance(window_snapshots, list)
        and isinstance(bucket_snapshots, list)
    ):
        return evidence_freshness(
            status="stale",
            reason_codes=["analysis_input_snapshot_invalid"],
            evidence_ref=evidence_ref,
            current_ref={"experiment_id": experiment_id},
        )
    try:
        expected_revisions = {
            str(item["revision_id"]): str(item["payload_hash"])
            for item in revision_snapshots
        }
        expected_windows = {
            str(item["window_id"]): str(item["payload_hash"])
            for item in window_snapshots
        }
        expected_buckets = {
            str(item["bucket_id"]): (
                str(item["payload_hash"]),
                int(item["version"]),
            )
            for item in bucket_snapshots
        }
        snapshot_record_version = int(experiment_snapshot.get("record_version", -1))
        if (
            len(expected_revisions) != len(revision_snapshots)
            or len(expected_windows) != len(window_snapshots)
            or len(expected_buckets) != len(bucket_snapshots)
        ):
            raise ValueError("duplicate_snapshot_identity")
    except (KeyError, TypeError, ValueError):
        return evidence_freshness(
            status="stale",
            reason_codes=["analysis_input_snapshot_invalid"],
            evidence_ref=evidence_ref,
            current_ref={"experiment_id": experiment_id},
        )

    expected_compact = {
        "experiment": {
            "payload_hash": str(experiment_snapshot.get("payload_hash")),
            "record_version": snapshot_record_version,
        },
        "revisions": sorted(expected_revisions.items()),
        "windows": sorted(expected_windows.items()),
        "buckets": sorted(
            (bucket_id, payload_hash, version)
            for bucket_id, (payload_hash, version) in expected_buckets.items()
        ),
    }
    evidence_ref["input_evidence_hash"] = payload_digest(expected_compact)

    with db.connect() as conn:
        experiment_row = conn.execute(
            """
            SELECT * FROM traffic_experiments
            WHERE tenant_id=? AND experiment_id=?
            """,
            (tenant_id, experiment_id),
        ).fetchone()
        if experiment_row is None:
            return evidence_freshness(
                status="stale",
                reason_codes=["experiment_evidence_changed"],
                evidence_ref=evidence_ref,
                current_ref={"experiment_id": experiment_id, "exists": False},
            )
        revision_ids = {
            str(experiment_row["control_revision_id"]),
            str(experiment_row["treatment_revision_id"]),
        }
        placeholders = ",".join("?" for _ in revision_ids)
        revision_rows = conn.execute(
            f"""
            SELECT id, payload_hash FROM listing_revisions
            WHERE tenant_id=? AND id IN ({placeholders})
            """,
            (tenant_id, *sorted(revision_ids)),
        ).fetchall()
        window_rows = conn.execute(
            """
            SELECT window_id, payload_hash FROM traffic_experiment_windows
            WHERE tenant_id=? AND experiment_id=?
            """,
            (tenant_id, experiment_id),
        ).fetchall()
        bucket_rows = conn.execute(
            f"""
            SELECT id, payload_hash, version FROM traffic_metric_buckets
            WHERE tenant_id=?
              AND connector_id<>?
              AND listing_revision_id IN ({placeholders})
              AND metric_end>? AND metric_start<?
            """,
            (
                tenant_id,
                LEGACY_UNSCOPED_CONNECTOR_ID,
                *sorted(revision_ids),
                experiment_row["started_at"],
                experiment_row["ended_at"],
            ),
        ).fetchall()
    current_revisions = {
        str(item["id"]): str(item["payload_hash"]) for item in revision_rows
    }
    current_windows = {
        str(item["window_id"]): str(item["payload_hash"]) for item in window_rows
    }
    current_buckets = {
        str(item["id"]): (str(item["payload_hash"]), int(item["version"]))
        for item in bucket_rows
    }
    current_compact = {
        "experiment": {
            "payload_hash": str(experiment_row["payload_hash"]),
            "record_version": int(experiment_row["record_version"]),
        },
        "revisions": sorted(current_revisions.items()),
        "windows": sorted(current_windows.items()),
        "buckets": sorted(
            (bucket_id, payload_hash, version)
            for bucket_id, (payload_hash, version) in current_buckets.items()
        ),
    }
    current_ref = {
        "experiment_id": experiment_id,
        "input_evidence_hash": payload_digest(current_compact),
    }
    reasons: list[str] = []
    if (
        experiment_row["ended_at"] is None
        or current_compact["experiment"] != expected_compact["experiment"]
    ):
        reasons.append("experiment_evidence_changed")
    if revision_ids != set(expected_revisions) or current_revisions != expected_revisions:
        reasons.append("listing_revision_evidence_changed")
    if current_windows != expected_windows:
        reasons.append("experiment_window_evidence_changed")
    if current_buckets != expected_buckets:
        reasons.append("traffic_metric_evidence_changed")
    return evidence_freshness(
        status="stale" if reasons else "current",
        reason_codes=reasons,
        evidence_ref=evidence_ref,
        current_ref=current_ref,
    )
