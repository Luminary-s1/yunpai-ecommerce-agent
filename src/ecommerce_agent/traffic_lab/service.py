from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from ..business.source_versioning import SourceVersionError, decide_write, payload_digest
from ..database import Database, utc_now
from .models import (
    CreativeAssetCreate,
    ListingRevisionCreate,
    TrafficAnalysisRunCreate,
    TrafficExperimentCreate,
    TrafficExperimentTransition,
    TrafficExperimentWindowCreate,
    TrafficMetricBucketUpsert,
)


class TrafficLabError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _canonical_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: str) -> Any:
    return json.loads(value)


class TrafficLabService:
    _METRIC_QUARANTINE_REASONS = {
        "listing_revision_missing",
        "listing_revision_not_found",
        "listing_revision_ambiguous",
        "metric_outside_revision_window",
    }
    _TRANSITIONS = {
        "draft": {"ready", "invalid"},
        "ready": {"running", "invalid"},
        "running": {"completed", "paused", "invalid"},
        "paused": {"running", "completed", "invalid"},
        "completed": set(),
        "invalid": set(),
    }

    def __init__(self, db: Database):
        self.db = db

    def register_asset(self, tenant_id: str, value: CreativeAssetCreate) -> dict[str, Any]:
        payload = value.model_dump(mode="json")
        payload_hash = payload_digest(payload)
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM creative_assets WHERE tenant_id=? AND sha256=?",
                (tenant_id, value.sha256),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise SourceVersionError("source_version_conflict")
                asset_id = str(existing["asset_id"])
                write_status = "idempotent"
            else:
                asset_id = f"asset-{uuid.uuid4().hex}"
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO creative_assets(
                        asset_id, tenant_id, sha256, mime_type, width, height,
                        storage_ref, source_ref, feature_schema_version,
                        payload_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id,
                        tenant_id,
                        value.sha256,
                        value.mime_type,
                        value.width,
                        value.height,
                        value.storage_ref,
                        value.source_ref,
                        value.feature_schema_version,
                        payload_hash,
                        now,
                        now,
                    ),
                )
        result = self.get_asset(tenant_id, asset_id)
        result["write_status"] = write_status
        return result

    def get_asset(self, tenant_id: str, asset_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM creative_assets WHERE tenant_id=? AND asset_id=?",
                (tenant_id, asset_id),
            ).fetchone()
        if row is None:
            raise TrafficLabError("creative_asset_not_found")
        return self._asset_view(dict(row))

    def list_assets(
        self, tenant_id: str, *, sha256: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if sha256 is not None:
            conditions.append("sha256=?")
            params.append(sha256.lower())
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM creative_assets
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC, asset_id DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._asset_view(dict(row)) for row in rows]

    def create_revision(
        self, tenant_id: str, value: ListingRevisionCreate
    ) -> dict[str, Any]:
        active_from = _canonical_time(value.active_from)
        active_to = _canonical_time(value.active_to) if value.active_to is not None else None
        source_updated_at = _canonical_time(value.source_updated_at)
        payload = value.model_dump(mode="json")
        payload.update(
            {
                "active_from": active_from,
                "active_to": active_to,
                "source_updated_at": source_updated_at,
            }
        )
        payload_hash = payload_digest(payload)
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            asset = conn.execute(
                "SELECT 1 FROM creative_assets WHERE tenant_id=? AND asset_id=?",
                (tenant_id, value.main_image_asset_id),
            ).fetchone()
            if asset is None:
                raise TrafficLabError("creative_asset_not_found")
            existing = conn.execute(
                """
                SELECT * FROM listing_revisions
                WHERE tenant_id=? AND connector_id=? AND store_id=?
                  AND item_id=? AND sku_id=? AND revision_no=?
                """,
                (
                    tenant_id,
                    value.connector_id,
                    value.store_id,
                    value.item_id,
                    value.sku_id,
                    value.revision_no,
                ),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise SourceVersionError("source_version_conflict")
                revision_id = str(existing["id"])
                write_status = "idempotent"
            else:
                latest = conn.execute(
                    """
                    SELECT revision_no, source_updated_at FROM listing_revisions
                    WHERE tenant_id=? AND connector_id=? AND store_id=?
                      AND item_id=? AND sku_id=?
                    ORDER BY revision_no DESC LIMIT 1
                    """,
                    (
                        tenant_id,
                        value.connector_id,
                        value.store_id,
                        value.item_id,
                        value.sku_id,
                    ),
                ).fetchone()
                if latest is not None and value.revision_no < int(latest["revision_no"]):
                    raise SourceVersionError("stale_source_version")
                if latest is not None:
                    latest_source_time = datetime.fromisoformat(
                        str(latest["source_updated_at"])
                    ).astimezone(UTC)
                    if value.source_updated_at.astimezone(UTC) < latest_source_time:
                        raise SourceVersionError("stale_source_version")
                revision_id = f"revision-{uuid.uuid4().hex}"
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO listing_revisions(
                        id, tenant_id, connector_id, store_id, item_id, sku_id,
                        revision_no, title, main_image_asset_id, sale_price,
                        attributes_json, active_from, active_to, source_updated_at,
                        payload_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        tenant_id,
                        value.connector_id,
                        value.store_id,
                        value.item_id,
                        value.sku_id,
                        value.revision_no,
                        value.title,
                        value.main_image_asset_id,
                        str(value.sale_price),
                        _json_dump(value.attributes),
                        active_from,
                        active_to,
                        source_updated_at,
                        payload_hash,
                        now,
                        now,
                    ),
                )
        result = self.get_revision(tenant_id, revision_id)
        result["write_status"] = write_status
        return result

    def get_revision(self, tenant_id: str, revision_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM listing_revisions WHERE tenant_id=? AND id=?",
                (tenant_id, revision_id),
            ).fetchone()
        if row is None:
            raise TrafficLabError("listing_revision_not_found")
        return self._revision_view(dict(row))

    def list_revisions(
        self,
        tenant_id: str,
        *,
        connector_id: str | None = None,
        store_id: str | None = None,
        item_id: str | None = None,
        sku_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        for column, value in (
            ("connector_id", connector_id),
            ("store_id", store_id),
            ("item_id", item_id),
            ("sku_id", sku_id),
        ):
            if value is not None:
                conditions.append(f"{column}=?")
                params.append(value)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM listing_revisions
                WHERE {' AND '.join(conditions)}
                ORDER BY revision_no DESC, active_from DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._revision_view(dict(row)) for row in rows]

    def revision_timeline_quality(
        self,
        tenant_id: str,
        *,
        connector_id: str,
        store_id: str,
        item_id: str,
        sku_id: str,
    ) -> dict[str, Any]:
        revisions = self.list_revisions(
            tenant_id,
            connector_id=connector_id,
            store_id=store_id,
            item_id=item_id,
            sku_id=sku_id,
            limit=10_000,
        )
        if not revisions:
            return {
                "quality": "invalid",
                "revision_count": 0,
                "issues": [{"code": "listing_revisions_missing"}],
            }
        revisions.sort(key=lambda item: (item["active_from"], item["revision_no"]))
        issues: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        coverage_end: datetime | None = None
        for current in revisions:
            current_start = datetime.fromisoformat(current["active_from"])
            if previous is not None:
                if coverage_end is None or current_start < coverage_end:
                    issues.append(
                        {
                            "code": "revision_window_overlap",
                            "previous_revision_id": previous["id"],
                            "revision_id": current["id"],
                        }
                    )
                elif current_start > coverage_end:
                    issues.append(
                        {
                            "code": "revision_window_gap",
                            "previous_revision_id": previous["id"],
                            "revision_id": current["id"],
                            "gap_start": coverage_end.isoformat(),
                            "gap_end": current["active_from"],
                        }
                    )
            current_end = (
                datetime.fromisoformat(current["active_to"])
                if current["active_to"] is not None
                else None
            )
            if previous is None or coverage_end is not None:
                if current_end is None or coverage_end is None:
                    coverage_end = current_end
                else:
                    coverage_end = max(coverage_end, current_end)
            previous = current
        issue_codes = {issue["code"] for issue in issues}
        quality = (
            "invalid"
            if "revision_window_overlap" in issue_codes
            else "degraded" if issues else "valid"
        )
        return {"quality": quality, "revision_count": len(revisions), "issues": issues}

    def upsert_metric_bucket(
        self, tenant_id: str, value: TrafficMetricBucketUpsert
    ) -> dict[str, Any]:
        metric_start = _canonical_time(value.metric_start)
        metric_end = _canonical_time(value.metric_end)
        data_as_of = _canonical_time(value.data_as_of)
        payload = value.model_dump(mode="json")
        payload.update(
            {
                "metric_start": metric_start,
                "metric_end": metric_end,
                "data_as_of": data_as_of,
            }
        )
        payload_hash = payload_digest(payload)
        quality_flags = self._metric_quality_flags(value)
        write_status = "applied"
        if value.listing_revision_id is None:
            raise TrafficLabError("listing_revision_missing")
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revision = conn.execute(
                "SELECT * FROM listing_revisions WHERE tenant_id=? AND id=?",
                (tenant_id, value.listing_revision_id),
            ).fetchone()
            if revision is None:
                raise TrafficLabError("listing_revision_not_found")
            if metric_start < str(revision["active_from"]) or (
                revision["active_to"] is not None
                and metric_end > str(revision["active_to"])
            ):
                raise TrafficLabError("metric_outside_revision_window")
            accepted_existing = conn.execute(
                "SELECT * FROM traffic_metric_buckets WHERE tenant_id=? AND source_id=?",
                (tenant_id, value.source_id),
            ).fetchone()
            quarantine_existing = conn.execute(
                """
                SELECT * FROM traffic_metric_quarantine
                WHERE tenant_id=? AND source_id=?
                """,
                (tenant_id, value.source_id),
            ).fetchone()
            if accepted_existing is not None and quarantine_existing is not None:
                raise TrafficLabError("metric_source_state_conflict")
            source_existing = accepted_existing or quarantine_existing
            if source_existing is not None:
                decision = decide_write(
                    existing_source_time=str(source_existing["data_as_of"]),
                    existing_payload_hash=str(source_existing["payload_hash"]),
                    incoming_source_time=data_as_of,
                    incoming_payload_hash=payload_hash,
                )
                bucket_id = (
                    str(accepted_existing["id"])
                    if accepted_existing is not None
                    else f"metric-{uuid.uuid4().hex}"
                )
                if decision == "idempotent" and accepted_existing is not None:
                    write_status = "idempotent"
                    version = int(source_existing["version"])
                else:
                    version = int(source_existing["version"])
                    if decision != "idempotent":
                        version += 1
            else:
                bucket_id = f"metric-{uuid.uuid4().hex}"
                version = 1
            if write_status == "applied":
                now = utc_now()
                created_at = (
                    str(source_existing["created_at"])
                    if source_existing is not None
                    else now
                )
                conn.execute(
                    """
                    INSERT INTO traffic_metric_buckets(
                        id, tenant_id, listing_revision_id, metric_start, metric_end,
                        bucket_granularity, traffic_source, impressions, clicks,
                        visitors, favorites, cart_adds, orders, sales_amount,
                        ad_spend, search_impressions, recommend_impressions,
                        data_as_of, source_id, payload_hash, quality_flags_json,
                        version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, source_id) DO UPDATE SET
                        listing_revision_id=excluded.listing_revision_id,
                        metric_start=excluded.metric_start,
                        metric_end=excluded.metric_end,
                        bucket_granularity=excluded.bucket_granularity,
                        traffic_source=excluded.traffic_source,
                        impressions=excluded.impressions,
                        clicks=excluded.clicks,
                        visitors=excluded.visitors,
                        favorites=excluded.favorites,
                        cart_adds=excluded.cart_adds,
                        orders=excluded.orders,
                        sales_amount=excluded.sales_amount,
                        ad_spend=excluded.ad_spend,
                        search_impressions=excluded.search_impressions,
                        recommend_impressions=excluded.recommend_impressions,
                        data_as_of=excluded.data_as_of,
                        payload_hash=excluded.payload_hash,
                        quality_flags_json=excluded.quality_flags_json,
                        version=excluded.version,
                        updated_at=excluded.updated_at
                    """,
                    (
                        bucket_id,
                        tenant_id,
                        value.listing_revision_id,
                        metric_start,
                        metric_end,
                        value.bucket_granularity,
                        value.traffic_source,
                        value.impressions,
                        value.clicks,
                        value.visitors,
                        value.favorites,
                        value.cart_adds,
                        value.orders,
                        str(value.sales_amount),
                        str(value.ad_spend),
                        value.search_impressions,
                        value.recommend_impressions,
                        data_as_of,
                        value.source_id,
                        payload_hash,
                        _json_dump(quality_flags),
                        version,
                        created_at,
                        now,
                    ),
                )
                if quarantine_existing is not None:
                    conn.execute(
                        """
                        DELETE FROM traffic_metric_quarantine
                        WHERE tenant_id=? AND source_id=?
                        """,
                        (tenant_id, value.source_id),
                    )
        result = self.get_metric_bucket(tenant_id, bucket_id)
        result["write_status"] = write_status
        return result

    def ingest_metric_bucket(
        self, tenant_id: str, value: TrafficMetricBucketUpsert
    ) -> dict[str, Any]:
        try:
            result = self.upsert_metric_bucket(tenant_id, value)
        except TrafficLabError as exc:
            if exc.code not in self._METRIC_QUARANTINE_REASONS:
                raise
            return self.quarantine_metric_bucket(tenant_id, value, reason_code=exc.code)
        result["disposition"] = "accepted"
        return result

    def quarantine_metric_bucket(
        self,
        tenant_id: str,
        value: TrafficMetricBucketUpsert,
        *,
        reason_code: str,
    ) -> dict[str, Any]:
        if reason_code not in self._METRIC_QUARANTINE_REASONS:
            raise TrafficLabError("metric_quarantine_reason_invalid")
        data_as_of = _canonical_time(value.data_as_of)
        payload = value.model_dump(mode="json")
        payload.update(
            {
                "metric_start": _canonical_time(value.metric_start),
                "metric_end": _canonical_time(value.metric_end),
                "data_as_of": data_as_of,
            }
        )
        payload_hash = payload_digest(payload)
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            quarantine_existing = conn.execute(
                """
                SELECT * FROM traffic_metric_quarantine
                WHERE tenant_id=? AND source_id=?
                """,
                (tenant_id, value.source_id),
            ).fetchone()
            accepted_existing = conn.execute(
                """
                SELECT * FROM traffic_metric_buckets
                WHERE tenant_id=? AND source_id=?
                """,
                (tenant_id, value.source_id),
            ).fetchone()
            if accepted_existing is not None and quarantine_existing is not None:
                raise TrafficLabError("metric_source_state_conflict")
            source_existing = quarantine_existing or accepted_existing
            if source_existing is not None:
                decision = decide_write(
                    existing_source_time=str(source_existing["data_as_of"]),
                    existing_payload_hash=str(source_existing["payload_hash"]),
                    incoming_source_time=data_as_of,
                    incoming_payload_hash=payload_hash,
                )
                quarantine_id = (
                    str(quarantine_existing["quarantine_id"])
                    if quarantine_existing is not None
                    else f"metric-quarantine-{uuid.uuid4().hex}"
                )
                if decision == "idempotent" and quarantine_existing is not None:
                    write_status = "idempotent"
                    version = int(source_existing["version"])
                else:
                    version = int(source_existing["version"])
                    if decision != "idempotent":
                        version += 1
            else:
                quarantine_id = f"metric-quarantine-{uuid.uuid4().hex}"
                version = 1
            if write_status == "applied":
                now = utc_now()
                created_at = (
                    str(source_existing["created_at"])
                    if source_existing is not None
                    else now
                )
                conn.execute(
                    """
                    INSERT INTO traffic_metric_quarantine(
                        quarantine_id, tenant_id, source_id, reason_code,
                        payload_json, data_as_of, payload_hash, version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, source_id) DO UPDATE SET
                        reason_code=excluded.reason_code,
                        payload_json=excluded.payload_json,
                        data_as_of=excluded.data_as_of,
                        payload_hash=excluded.payload_hash,
                        version=excluded.version,
                        updated_at=excluded.updated_at
                    """,
                    (
                        quarantine_id,
                        tenant_id,
                        value.source_id,
                        reason_code,
                        _json_dump(payload),
                        data_as_of,
                        payload_hash,
                        version,
                        created_at,
                        now,
                    ),
                )
                if accepted_existing is not None:
                    conn.execute(
                        """
                        DELETE FROM traffic_metric_buckets
                        WHERE tenant_id=? AND source_id=?
                        """,
                        (tenant_id, value.source_id),
                    )
        result = self.get_metric_quarantine(tenant_id, quarantine_id)
        result["write_status"] = write_status
        result["disposition"] = "quarantined"
        return result

    def get_metric_quarantine(
        self, tenant_id: str, quarantine_id: str
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM traffic_metric_quarantine
                WHERE tenant_id=? AND quarantine_id=?
                """,
                (tenant_id, quarantine_id),
            ).fetchone()
        if row is None:
            raise TrafficLabError("traffic_metric_quarantine_not_found")
        return self._metric_quarantine_view(dict(row))

    def list_metric_quarantine(
        self, tenant_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM traffic_metric_quarantine
                WHERE tenant_id=?
                ORDER BY data_as_of DESC, quarantine_id DESC LIMIT ?
                """,
                (tenant_id, limit),
            ).fetchall()
        return [self._metric_quarantine_view(dict(row)) for row in rows]

    def get_metric_bucket(self, tenant_id: str, bucket_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM traffic_metric_buckets WHERE tenant_id=? AND id=?",
                (tenant_id, bucket_id),
            ).fetchone()
        if row is None:
            raise TrafficLabError("traffic_metric_bucket_not_found")
        return self._metric_view(dict(row))

    def list_metric_buckets(
        self,
        tenant_id: str,
        *,
        listing_revision_id: str | None = None,
        traffic_source: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if listing_revision_id is not None:
            conditions.append("listing_revision_id=?")
            params.append(listing_revision_id)
        if traffic_source is not None:
            conditions.append("traffic_source=?")
            params.append(traffic_source)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM traffic_metric_buckets
                WHERE {' AND '.join(conditions)}
                ORDER BY metric_start DESC, id DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._metric_view(dict(row)) for row in rows]

    def trace_metric_bucket(self, tenant_id: str, bucket_id: str) -> dict[str, Any]:
        metric = self.get_metric_bucket(tenant_id, bucket_id)
        revision = self.get_revision(tenant_id, metric["listing_revision_id"])
        asset = self.get_asset(tenant_id, revision["main_image_asset_id"])
        return {
            "metric": metric,
            "revision": {
                "id": revision["id"],
                "revision_no": revision["revision_no"],
                "title": revision["title"],
                "main_image_asset_id": revision["main_image_asset_id"],
                "sale_price": revision["sale_price"],
                "active_from": revision["active_from"],
                "active_to": revision["active_to"],
            },
            "asset": {
                "asset_id": asset["asset_id"],
                "sha256": asset["sha256"],
                "mime_type": asset["mime_type"],
                "width": asset["width"],
                "height": asset["height"],
                "storage_ref": asset["storage_ref"],
            },
        }

    def create_experiment(
        self, tenant_id: str, value: TrafficExperimentCreate
    ) -> dict[str, Any]:
        started_at = _canonical_time(value.started_at)
        ended_at = _canonical_time(value.ended_at) if value.ended_at is not None else None
        payload = value.model_dump(mode="json")
        payload.update({"started_at": started_at, "ended_at": ended_at})
        payload_hash = payload_digest(payload)
        experiment_id = f"experiment-{uuid.uuid4().hex}"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            control = conn.execute(
                "SELECT * FROM listing_revisions WHERE tenant_id=? AND id=?",
                (tenant_id, value.control_revision_id),
            ).fetchone()
            treatment = conn.execute(
                "SELECT * FROM listing_revisions WHERE tenant_id=? AND id=?",
                (tenant_id, value.treatment_revision_id),
            ).fetchone()
            if control is None or treatment is None:
                raise TrafficLabError("listing_revision_not_found")
            for row in (control, treatment):
                if row["store_id"] != value.store_id or row["sku_id"] != value.sku_id:
                    raise TrafficLabError("revision_scope_mismatch")
            now = utc_now()
            conn.execute(
                """
                INSERT INTO traffic_experiments(
                    experiment_id, tenant_id, store_id, sku_id, experiment_type,
                    primary_metric, status, started_at, ended_at,
                    control_revision_id, treatment_revision_id, minimum_exposure,
                    washout_window, analysis_policy_version, payload_hash,
                    record_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    experiment_id,
                    tenant_id,
                    value.store_id,
                    value.sku_id,
                    value.experiment_type,
                    value.primary_metric,
                    started_at,
                    ended_at,
                    value.control_revision_id,
                    value.treatment_revision_id,
                    value.minimum_exposure,
                    value.washout_window,
                    value.analysis_policy_version,
                    payload_hash,
                    now,
                    now,
                ),
            )
        result = self.get_experiment(tenant_id, experiment_id)
        result["write_status"] = "applied"
        return result

    def get_experiment(self, tenant_id: str, experiment_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM traffic_experiments WHERE tenant_id=? AND experiment_id=?",
                (tenant_id, experiment_id),
            ).fetchone()
        if row is None:
            raise TrafficLabError("traffic_experiment_not_found")
        return self._experiment_view(dict(row))

    def list_experiments(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        sku_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        for column, value in (("store_id", store_id), ("sku_id", sku_id), ("status", status)):
            if value is not None:
                conditions.append(f"{column}=?")
                params.append(value)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM traffic_experiments
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC, experiment_id DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._experiment_view(dict(row)) for row in rows]

    def transition_experiment(
        self,
        tenant_id: str,
        experiment_id: str,
        value: TrafficExperimentTransition,
    ) -> dict[str, Any]:
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM traffic_experiments WHERE tenant_id=? AND experiment_id=?",
                (tenant_id, experiment_id),
            ).fetchone()
            if existing is None:
                raise TrafficLabError("traffic_experiment_not_found")
            current_version = int(existing["record_version"])
            if value.expected_version is not None and value.expected_version != current_version:
                raise TrafficLabError("experiment_version_conflict")
            current_status = str(existing["status"])
            if current_status == value.status:
                write_status = "idempotent"
            elif value.status not in self._TRANSITIONS[current_status]:
                raise TrafficLabError("invalid_experiment_transition")
            else:
                ended_at = (
                    _canonical_time(value.ended_at)
                    if value.ended_at is not None
                    else existing["ended_at"]
                )
                if value.status == "completed" and ended_at is None:
                    raise TrafficLabError("experiment_end_required")
                if ended_at is not None and ended_at <= str(existing["started_at"]):
                    raise TrafficLabError("experiment_window_invalid")
                conn.execute(
                    """
                    UPDATE traffic_experiments
                    SET status=?, ended_at=?, record_version=record_version+1, updated_at=?
                    WHERE tenant_id=? AND experiment_id=? AND record_version=?
                    """,
                    (
                        value.status,
                        ended_at,
                        utc_now(),
                        tenant_id,
                        experiment_id,
                        current_version,
                    ),
                )
        result = self.get_experiment(tenant_id, experiment_id)
        result["write_status"] = write_status
        return result

    def add_experiment_window(
        self,
        tenant_id: str,
        experiment_id: str,
        value: TrafficExperimentWindowCreate,
    ) -> dict[str, Any]:
        window_start = _canonical_time(value.window_start)
        window_end = _canonical_time(value.window_end)
        payload = value.model_dump(mode="json")
        payload.update({"window_start": window_start, "window_end": window_end})
        payload_hash = payload_digest(payload)
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            experiment = conn.execute(
                "SELECT * FROM traffic_experiments WHERE tenant_id=? AND experiment_id=?",
                (tenant_id, experiment_id),
            ).fetchone()
            if experiment is None:
                raise TrafficLabError("traffic_experiment_not_found")
            expected_revision_id = experiment[f"{value.assignment}_revision_id"]
            if value.listing_revision_id != expected_revision_id:
                raise TrafficLabError("window_assignment_revision_mismatch")
            if window_start < str(experiment["started_at"]) or (
                experiment["ended_at"] is not None
                and window_end > str(experiment["ended_at"])
            ):
                raise TrafficLabError("experiment_window_out_of_bounds")
            if value.source_receipt_id is not None:
                existing = conn.execute(
                    """
                    SELECT * FROM traffic_experiment_windows
                    WHERE tenant_id=? AND experiment_id=? AND source_receipt_id=?
                    """,
                    (tenant_id, experiment_id, value.source_receipt_id),
                ).fetchone()
            else:
                existing = conn.execute(
                    """
                    SELECT * FROM traffic_experiment_windows
                    WHERE tenant_id=? AND experiment_id=? AND window_start=?
                      AND window_end=? AND assignment=?
                    """,
                    (tenant_id, experiment_id, window_start, window_end, value.assignment),
                ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise SourceVersionError("source_version_conflict")
                window_id = str(existing["window_id"])
                write_status = "idempotent"
            else:
                window_id = f"window-{uuid.uuid4().hex}"
                now = utc_now()
                try:
                    conn.execute(
                        """
                        INSERT INTO traffic_experiment_windows(
                            window_id, tenant_id, experiment_id, listing_revision_id,
                            window_start, window_end, assignment, washout,
                            source_receipt_id, payload_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            window_id,
                            tenant_id,
                            experiment_id,
                            value.listing_revision_id,
                            window_start,
                            window_end,
                            value.assignment,
                            int(value.washout),
                            value.source_receipt_id,
                            payload_hash,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    if "UNIQUE constraint failed" in str(exc):
                        raise SourceVersionError("source_version_conflict") from exc
                    raise
        result = self._get_experiment_window(tenant_id, window_id)
        result["write_status"] = write_status
        return result

    def list_experiment_windows(
        self, tenant_id: str, experiment_id: str, *, limit: int = 10_000
    ) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM traffic_experiment_windows
                WHERE tenant_id=? AND experiment_id=?
                ORDER BY window_start ASC, window_id ASC LIMIT ?
                """,
                (tenant_id, experiment_id, limit),
            ).fetchall()
        return [self._window_view(dict(row)) for row in rows]

    def experiment_window_quality(
        self, tenant_id: str, experiment_id: str
    ) -> dict[str, Any]:
        experiment = self.get_experiment(tenant_id, experiment_id)
        windows = self.list_experiment_windows(tenant_id, experiment_id)
        if not windows:
            return {
                "quality": "invalid",
                "window_count": 0,
                "issues": [{"code": "experiment_windows_missing"}],
            }
        issues: list[dict[str, Any]] = []
        experiment_start = datetime.fromisoformat(experiment["started_at"])
        experiment_end = (
            datetime.fromisoformat(experiment["ended_at"])
            if experiment["ended_at"] is not None
            else None
        )
        coverage_end = experiment_start
        previous_window_id: str | None = None
        for window in windows:
            start = datetime.fromisoformat(window["window_start"])
            end = datetime.fromisoformat(window["window_end"])
            if start < coverage_end:
                issues.append(
                    {
                        "code": "experiment_window_overlap",
                        "previous_window_id": previous_window_id,
                        "window_id": window["window_id"],
                    }
                )
            elif start > coverage_end:
                issues.append(
                    {
                        "code": "experiment_window_gap",
                        "gap_start": coverage_end.isoformat(),
                        "gap_end": window["window_start"],
                    }
                )
            if window["source_receipt_id"] is None:
                issues.append(
                    {"code": "source_receipt_missing", "window_id": window["window_id"]}
                )
            coverage_end = max(coverage_end, end)
            previous_window_id = window["window_id"]
        if experiment_end is not None and coverage_end < experiment_end:
            issues.append(
                {
                    "code": "experiment_window_gap",
                    "gap_start": coverage_end.isoformat(),
                    "gap_end": experiment_end.isoformat(),
                }
            )
        issue_codes = {issue["code"] for issue in issues}
        quality = (
            "invalid"
            if "experiment_window_overlap" in issue_codes
            else "degraded" if issues else "valid"
        )
        return {"quality": quality, "window_count": len(windows), "issues": issues}

    def create_analysis_run(
        self,
        tenant_id: str,
        experiment_id: str,
        value: TrafficAnalysisRunCreate,
    ) -> dict[str, Any]:
        payload = value.model_dump(mode="json")
        payload_hash = payload_digest(payload)
        analysis_run_id = f"analysis-{uuid.uuid4().hex}"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            experiment = conn.execute(
                "SELECT 1 FROM traffic_experiments WHERE tenant_id=? AND experiment_id=?",
                (tenant_id, experiment_id),
            ).fetchone()
            if experiment is None:
                raise TrafficLabError("traffic_experiment_not_found")
            now = utc_now()
            conn.execute(
                """
                INSERT INTO traffic_analysis_runs(
                    analysis_run_id, tenant_id, experiment_id, method,
                    data_window_json, sample_size_json, effect_estimate_json,
                    confidence_interval_json, evidence_json, counter_evidence_json,
                    hypotheses_json, model_provider, model_name, prompt_version,
                    analysis_code_version, payload_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis_run_id,
                    tenant_id,
                    experiment_id,
                    value.method,
                    _json_dump(value.data_window),
                    _json_dump(value.sample_size),
                    _json_dump(value.effect_estimate),
                    _json_dump(value.confidence_interval),
                    _json_dump(value.evidence),
                    _json_dump(value.counter_evidence),
                    _json_dump(value.hypotheses),
                    value.model_provider,
                    value.model_name,
                    value.prompt_version,
                    value.analysis_code_version,
                    payload_hash,
                    now,
                    now,
                ),
            )
        result = self.get_analysis_run(tenant_id, analysis_run_id)
        result["write_status"] = "applied"
        return result

    def get_analysis_run(self, tenant_id: str, analysis_run_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM traffic_analysis_runs
                WHERE tenant_id=? AND analysis_run_id=?
                """,
                (tenant_id, analysis_run_id),
            ).fetchone()
        if row is None:
            raise TrafficLabError("traffic_analysis_run_not_found")
        return self._analysis_view(dict(row))

    def list_analysis_runs(
        self, tenant_id: str, experiment_id: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM traffic_analysis_runs
                WHERE tenant_id=? AND experiment_id=?
                ORDER BY created_at DESC, analysis_run_id DESC LIMIT ?
                """,
                (tenant_id, experiment_id, limit),
            ).fetchall()
        return [self._analysis_view(dict(row)) for row in rows]

    def _get_experiment_window(self, tenant_id: str, window_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM traffic_experiment_windows
                WHERE tenant_id=? AND window_id=?
                """,
                (tenant_id, window_id),
            ).fetchone()
        if row is None:
            raise TrafficLabError("traffic_experiment_window_not_found")
        return self._window_view(dict(row))

    @staticmethod
    def _metric_quality_flags(value: TrafficMetricBucketUpsert) -> list[str]:
        flags: list[str] = []
        if value.orders > 0 and value.sales_amount == 0:
            flags.append("orders_without_sales_amount")
        if value.orders == 0 and value.sales_amount > 0:
            flags.append("sales_without_orders")
        return flags

    @staticmethod
    def _asset_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "asset_id": row["asset_id"],
            "sha256": row["sha256"],
            "mime_type": row["mime_type"],
            "width": row["width"],
            "height": row["height"],
            "storage_ref": row["storage_ref"],
            "source_ref": row["source_ref"],
            "feature_schema_version": row["feature_schema_version"],
            "payload_hash": row["payload_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _revision_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "connector_id": row["connector_id"],
            "store_id": row["store_id"],
            "item_id": row["item_id"],
            "sku_id": row["sku_id"],
            "revision_no": row["revision_no"],
            "title": row["title"],
            "main_image_asset_id": row["main_image_asset_id"],
            "sale_price": row["sale_price"],
            "attributes": _json_load(row["attributes_json"]),
            "active_from": row["active_from"],
            "active_to": row["active_to"],
            "source_updated_at": row["source_updated_at"],
            "payload_hash": row["payload_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _metric_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "listing_revision_id": row["listing_revision_id"],
            "metric_start": row["metric_start"],
            "metric_end": row["metric_end"],
            "bucket_granularity": row["bucket_granularity"],
            "traffic_source": row["traffic_source"],
            "impressions": row["impressions"],
            "clicks": row["clicks"],
            "visitors": row["visitors"],
            "favorites": row["favorites"],
            "cart_adds": row["cart_adds"],
            "orders": row["orders"],
            "sales_amount": row["sales_amount"],
            "ad_spend": row["ad_spend"],
            "search_impressions": row["search_impressions"],
            "recommend_impressions": row["recommend_impressions"],
            "data_as_of": row["data_as_of"],
            "source_id": row["source_id"],
            "payload_hash": row["payload_hash"],
            "quality_flags": _json_load(row["quality_flags_json"]),
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _metric_quarantine_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "quarantine_id": row["quarantine_id"],
            "source_id": row["source_id"],
            "reason_code": row["reason_code"],
            "payload": _json_load(row["payload_json"]),
            "data_as_of": row["data_as_of"],
            "payload_hash": row["payload_hash"],
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _experiment_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "experiment_id": row["experiment_id"],
            "store_id": row["store_id"],
            "sku_id": row["sku_id"],
            "experiment_type": row["experiment_type"],
            "primary_metric": row["primary_metric"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "control_revision_id": row["control_revision_id"],
            "treatment_revision_id": row["treatment_revision_id"],
            "minimum_exposure": row["minimum_exposure"],
            "washout_window": row["washout_window"],
            "analysis_policy_version": row["analysis_policy_version"],
            "payload_hash": row["payload_hash"],
            "record_version": row["record_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _window_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "window_id": row["window_id"],
            "experiment_id": row["experiment_id"],
            "listing_revision_id": row["listing_revision_id"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "assignment": row["assignment"],
            "washout": bool(row["washout"]),
            "source_receipt_id": row["source_receipt_id"],
            "payload_hash": row["payload_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _analysis_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "analysis_run_id": row["analysis_run_id"],
            "experiment_id": row["experiment_id"],
            "method": row["method"],
            "data_window": _json_load(row["data_window_json"]),
            "sample_size": _json_load(row["sample_size_json"]),
            "effect_estimate": _json_load(row["effect_estimate_json"]),
            "confidence_interval": _json_load(row["confidence_interval_json"]),
            "evidence": _json_load(row["evidence_json"]),
            "counter_evidence": _json_load(row["counter_evidence_json"]),
            "hypotheses": _json_load(row["hypotheses_json"]),
            "model_provider": row["model_provider"],
            "model_name": row["model_name"],
            "prompt_version": row["prompt_version"],
            "analysis_code_version": row["analysis_code_version"],
            "payload_hash": row["payload_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
