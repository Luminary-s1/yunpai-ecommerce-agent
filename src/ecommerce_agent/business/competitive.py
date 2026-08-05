from __future__ import annotations

import json
import re
import unicodedata
import uuid
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..database import Database, utc_now
from ..text_utils import redact_sensitive
from .source_versioning import canonical_source_time, decide_write, payload_digest


CompetitorSource = Literal[
    "authorized_api", "licensed_provider", "manual", "file_import", "virtual"
]
CompetitiveAlertStatus = Literal["open", "acknowledged", "resolved"]
CompetitiveMatchStatus = Literal["pending", "approved", "rejected"]
CompetitiveSignalType = Literal["product_claim", "review_summary"]


class CompetitiveCustomDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=64)
    value_type: Literal["text", "number", "boolean"]
    value_text: str | None = Field(default=None, max_length=200)
    value_number: Decimal | None = None
    value_boolean: bool | None = None
    unit: str | None = Field(default=None, max_length=32)

    @field_validator("key", "label", "value_text", "unit")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("custom dimension text cannot be blank")
        return normalized

    @field_validator("key")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("custom dimension key cannot contain control characters")
        return value

    @model_validator(mode="after")
    def validate_typed_value(self) -> "CompetitiveCustomDimension":
        fields = {
            "text": self.value_text,
            "number": self.value_number,
            "boolean": self.value_boolean,
        }
        if fields[self.value_type] is None or any(
            value is not None
            for value_type, value in fields.items()
            if value_type != self.value_type
        ):
            raise ValueError("custom dimension must provide exactly its typed value")
        if self.value_type != "number" and self.unit is not None:
            raise ValueError("custom dimension unit is only valid for number values")
        return self


class CompetitiveProductIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=500)
    brand: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=128)
    category: str | None = Field(default=None, max_length=200)
    gtin: str | None = Field(default=None, max_length=64)
    attributes: dict[str, str] = Field(default_factory=dict)
    custom_dimensions: list[CompetitiveCustomDimension] = Field(
        default_factory=list,
        max_length=32,
    )

    @field_validator("gtin")
    @classmethod
    def validate_gtin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized.isdigit() or len(normalized) not in {8, 12, 13, 14}:
            raise ValueError("gtin must contain 8, 12, 13, or 14 digits")
        return normalized

    @field_validator("attributes")
    @classmethod
    def validate_attributes(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("identity attributes cannot exceed 32 entries")
        cleaned: dict[str, str] = {}
        for key, item in value.items():
            normalized_key = str(key).strip()
            normalized_value = str(item).strip()
            if not normalized_key or not normalized_value:
                raise ValueError("identity attributes require non-empty keys and values")
            if len(normalized_key) > 64 or len(normalized_value) > 200:
                raise ValueError("identity attribute exceeds length limit")
            cleaned[normalized_key] = normalized_value
        return cleaned

    @field_validator("custom_dimensions")
    @classmethod
    def validate_custom_dimensions(
        cls,
        value: list[CompetitiveCustomDimension],
    ) -> list[CompetitiveCustomDimension]:
        keys = [dimension.key.casefold() for dimension in value]
        if len(keys) != len(set(keys)):
            raise ValueError("custom dimension keys must be unique")
        return value


class CompetitiveEntityMatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    subject_sku: str = Field(min_length=1, max_length=128)
    competitor_name: str = Field(min_length=1, max_length=200)
    competitor_sku: str = Field(min_length=1, max_length=128)
    subject_identity: CompetitiveProductIdentity
    competitor_identity: CompetitiveProductIdentity
    comparison_keys: list[str] = Field(default_factory=list, max_length=20)
    source_type: CompetitorSource
    source_ref: str = Field(min_length=4, max_length=500)
    source_id: str = Field(min_length=1, max_length=256)
    is_estimate: bool = True
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def require_aware_observed_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value

    @field_validator("comparison_keys")
    @classmethod
    def normalize_comparison_keys(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for key in value:
            normalized = str(key).strip()
            if not normalized or len(normalized) > 64:
                raise ValueError("comparison keys must be 1-64 characters")
            folded = normalized.casefold()
            if folded not in seen:
                result.append(normalized)
                seen.add(folded)
        return result

    @model_validator(mode="after")
    def protect_virtual_provenance(self) -> "CompetitiveEntityMatchCreate":
        if self.source_type == "virtual" and not self.is_estimate:
            raise ValueError("virtual entity matches must be marked as estimates")
        return self


class CompetitiveMatchTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_status: Literal["approved", "rejected"]
    expected_record_version: int = Field(ge=1)
    note: str = Field(min_length=8, max_length=500)


class CompetitiveSignalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_id: str = Field(min_length=1, max_length=128)
    connector_id: str = Field(min_length=1, max_length=128)
    entity_role: Literal["subject", "competitor"]
    signal_type: CompetitiveSignalType
    aspect: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=2, max_length=2000)
    sample_size: int | None = Field(default=None, ge=5, le=10_000_000)
    positive_count: int | None = Field(default=None, ge=0, le=10_000_000)
    negative_count: int | None = Field(default=None, ge=0, le=10_000_000)
    source_type: CompetitorSource
    source_ref: str = Field(min_length=4, max_length=500)
    source_id: str = Field(min_length=1, max_length=256)
    is_estimate: bool = True
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def require_aware_observed_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value

    @model_validator(mode="after")
    def validate_aggregate_signal(self) -> "CompetitiveSignalCreate":
        if self.source_type == "virtual" and not self.is_estimate:
            raise ValueError("virtual competitive signals must be marked as estimates")
        if self.signal_type == "review_summary":
            if self.sample_size is None:
                raise ValueError("review summaries require sample_size")
            positive = self.positive_count or 0
            negative = self.negative_count or 0
            if positive + negative > self.sample_size:
                raise ValueError("review sentiment counts cannot exceed sample_size")
        elif any(
            item is not None
            for item in (self.sample_size, self.positive_count, self.negative_count)
        ):
            raise ValueError("product claims cannot contain review sample counts")
        return self


class CompetitorObservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    subject_sku: str = Field(min_length=1, max_length=128)
    competitor_name: str = Field(min_length=1, max_length=200)
    competitor_sku: str = Field(min_length=1, max_length=128)
    subject_price: Decimal = Field(gt=0)
    competitor_price: Decimal = Field(gt=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    rating_value: Decimal | None = Field(default=None, ge=0)
    rating_scale: Decimal | None = Field(default=None, gt=0)
    sales_rank: int | None = Field(default=None, ge=1)
    rank_scope: str | None = Field(default=None, min_length=1, max_length=200)
    source_type: CompetitorSource
    source_ref: str = Field(min_length=4, max_length=500)
    is_estimate: bool = True
    observed_at: datetime
    source_id: str | None = Field(default=None, max_length=256)
    entity_match_id: str | None = Field(default=None, max_length=128)

    @field_validator("observed_at")
    @classmethod
    def require_aware_observed_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value

    @field_validator("rank_scope")
    @classmethod
    def normalize_rank_scope(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("rank_scope cannot be blank")
        return normalized

    @model_validator(mode="after")
    def protect_virtual_provenance(self) -> "CompetitorObservationCreate":
        if self.source_type == "virtual" and not self.is_estimate:
            raise ValueError("virtual competitor observations must be marked as estimates")
        if (self.rating_value is None) != (self.rating_scale is None):
            raise ValueError("rating_value and rating_scale must be provided together")
        if (
            self.rating_value is not None
            and self.rating_scale is not None
            and self.rating_value > self.rating_scale
        ):
            raise ValueError("rating_value cannot exceed rating_scale")
        if (self.sales_rank is None) != (self.rank_scope is None):
            raise ValueError("sales_rank and rank_scope must be provided together")
        return self


class CompetitiveMonitorUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    subject_sku: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    undercut_threshold_percent: Decimal = Field(default=Decimal("5.00"), gt=0, le=100)
    price_drop_threshold_percent: Decimal = Field(default=Decimal("5.00"), gt=0, le=100)
    stale_after_hours: int = Field(default=24, ge=1, le=8760)
    include_estimates: bool = False
    require_approved_match: bool = True
    expected_record_version: int = Field(default=0, ge=0)


class CompetitiveAlertTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_status: Literal["acknowledged", "resolved"]
    expected_record_version: int = Field(ge=1)
    note: str = Field(min_length=2, max_length=500)


class CompetitiveIntelligenceService:
    def __init__(self, db: Database):
        self.db = db

    def record_entity_match(
        self,
        tenant_id: str,
        value: CompetitiveEntityMatchCreate,
    ) -> dict[str, Any]:
        observed_at = canonical_source_time(value.observed_at)
        payload = value.model_dump(mode="json")
        payload["observed_at"] = observed_at
        payload_hash = payload_digest(payload)
        assessment = self._assess_entity_match(value)
        match_id = f"compmatch-{uuid.uuid4().hex}"
        now = utc_now()
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM competitive_entity_matches
                WHERE tenant_id=? AND connector_id=? AND source_id=?
                """,
                (tenant_id, value.connector_id, value.source_id),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise ValueError("competitive_match_version_conflict")
                match_id = str(existing["id"])
                write_status = "idempotent"
            else:
                conn.execute(
                    """
                    INSERT INTO competitive_entity_matches(
                        id, tenant_id, connector_id, store_id, subject_sku,
                        competitor_name, competitor_sku, source_type, source_ref,
                        source_id, is_estimate, observed_at, subject_identity_json,
                        competitor_identity_json, comparison_keys_json, score,
                        matched_fields_json, conflicts_json, missing_fields_json,
                        recommended_status, status, payload_hash, record_version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'pending', ?, 1, ?, ?)
                    """,
                    (
                        match_id,
                        tenant_id,
                        value.connector_id,
                        value.store_id,
                        value.subject_sku,
                        value.competitor_name,
                        value.competitor_sku,
                        value.source_type,
                        value.source_ref,
                        value.source_id,
                        int(value.is_estimate),
                        observed_at,
                        json.dumps(
                            value.subject_identity.model_dump(mode="json"),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps(
                            value.competitor_identity.model_dump(mode="json"),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        json.dumps(value.comparison_keys, ensure_ascii=False),
                        assessment["score"],
                        json.dumps(
                            assessment["matched_fields"], ensure_ascii=False, sort_keys=True
                        ),
                        json.dumps(
                            assessment["conflicts"], ensure_ascii=False, sort_keys=True
                        ),
                        json.dumps(
                            assessment["missing_fields"], ensure_ascii=False
                        ),
                        assessment["recommended_status"],
                        payload_hash,
                        now,
                        now,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM competitive_entity_matches WHERE id=? AND tenant_id=?",
                (match_id, tenant_id),
            ).fetchone()
        result = self._match_view(dict(row))
        result["write_status"] = write_status
        return result

    def list_entity_matches(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        subject_sku: str | None = None,
        status: CompetitiveMatchStatus | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if subject_sku:
            conditions.append("subject_sku=?")
            params.append(subject_sku)
        if status:
            conditions.append("status=?")
            params.append(status)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM competitive_entity_matches
                WHERE {' AND '.join(conditions)}
                ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'rejected' THEN 1 ELSE 2 END,
                         updated_at DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [self._match_view(dict(row)) for row in rows]

    def get_entity_match(self, tenant_id: str, match_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM competitive_entity_matches WHERE id=? AND tenant_id=?",
                (match_id, tenant_id),
            ).fetchone()
            if row is None:
                raise ValueError("competitive_match_not_found")
            decisions = conn.execute(
                """
                SELECT * FROM competitive_match_decisions
                WHERE tenant_id=? AND match_id=? ORDER BY created_at DESC
                """,
                (tenant_id, match_id),
            ).fetchall()
        result = self._match_view(dict(row))
        result["decisions"] = [self._match_decision_view(dict(item)) for item in decisions]
        return result

    def transition_entity_match(
        self,
        tenant_id: str,
        match_id: str,
        value: CompetitiveMatchTransition,
        *,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            raw = conn.execute(
                "SELECT * FROM competitive_entity_matches WHERE id=? AND tenant_id=?",
                (match_id, tenant_id),
            ).fetchone()
            if raw is None:
                raise ValueError("competitive_match_not_found")
            current = dict(raw)
            if int(current["record_version"]) != value.expected_record_version:
                raise ValueError("competitive_match_version_conflict")
            if str(current["status"]) == value.target_status:
                raise ValueError("competitive_match_transition_invalid")
            conflicts = self._json_list(current.get("conflicts_json"))
            if value.target_status == "approved" and (
                current["recommended_status"] != "approved" or conflicts
            ):
                raise ValueError("competitive_match_not_approvable")
            next_version = int(current["record_version"]) + 1
            changed = conn.execute(
                """
                UPDATE competitive_entity_matches
                SET status=?, reviewed_by=?, reviewed_at=?, review_note=?,
                    record_version=?, updated_at=?
                WHERE id=? AND tenant_id=? AND record_version=?
                """,
                (
                    value.target_status,
                    actor,
                    now,
                    value.note,
                    next_version,
                    now,
                    match_id,
                    tenant_id,
                    value.expected_record_version,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("competitive_match_version_conflict")
            conn.execute(
                """
                INSERT INTO competitive_match_decisions(
                    id, tenant_id, match_id, from_status, to_status,
                    match_record_version, actor, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"compdecision-{uuid.uuid4().hex}",
                    tenant_id,
                    match_id,
                    current["status"],
                    value.target_status,
                    next_version,
                    actor,
                    value.note,
                    now,
                ),
            )
            changed_row = conn.execute(
                "SELECT * FROM competitive_entity_matches WHERE id=? AND tenant_id=?",
                (match_id, tenant_id),
            ).fetchone()
        result = self._match_view(dict(changed_row))
        result["alert_evaluation"] = self.evaluate_scope(
            tenant_id,
            store_id=str(result["store_id"]),
            subject_sku=str(result["subject_sku"]),
        )
        return result

    def record_signal(
        self,
        tenant_id: str,
        value: CompetitiveSignalCreate,
    ) -> dict[str, Any]:
        observed_at = canonical_source_time(value.observed_at)
        payload = value.model_dump(mode="json")
        payload["observed_at"] = observed_at
        payload_hash = payload_digest(payload)
        safe_summary, redacted = redact_sensitive(value.summary)
        signal_id = f"compsignal-{uuid.uuid4().hex}"
        now = utc_now()
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            match_row = conn.execute(
                "SELECT * FROM competitive_entity_matches WHERE id=? AND tenant_id=?",
                (value.match_id, tenant_id),
            ).fetchone()
            if match_row is None:
                raise ValueError("competitive_match_not_found")
            match = dict(match_row)
            existing = conn.execute(
                """
                SELECT * FROM competitive_signals
                WHERE tenant_id=? AND connector_id=? AND source_id=?
                """,
                (tenant_id, value.connector_id, value.source_id),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise ValueError("competitive_signal_version_conflict")
                signal_id = str(existing["id"])
                write_status = "idempotent"
            else:
                conn.execute(
                    """
                    INSERT INTO competitive_signals(
                        id, tenant_id, match_id, connector_id, store_id, subject_sku,
                        competitor_name, competitor_sku, entity_role, signal_type,
                        aspect, summary_redacted, sample_size, positive_count,
                        negative_count, source_type, source_ref, source_id,
                        is_estimate, observed_at, payload_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal_id,
                        tenant_id,
                        value.match_id,
                        value.connector_id,
                        match["store_id"],
                        match["subject_sku"],
                        match["competitor_name"],
                        match["competitor_sku"],
                        value.entity_role,
                        value.signal_type,
                        value.aspect,
                        safe_summary,
                        value.sample_size,
                        value.positive_count,
                        value.negative_count,
                        value.source_type,
                        value.source_ref,
                        value.source_id,
                        int(value.is_estimate),
                        observed_at,
                        payload_hash,
                        now,
                    ),
                )
            row = conn.execute(
                """
                SELECT s.*, m.status AS match_status, m.score AS match_score
                FROM competitive_signals s
                JOIN competitive_entity_matches m ON m.id=s.match_id
                WHERE s.id=? AND s.tenant_id=?
                """,
                (signal_id, tenant_id),
            ).fetchone()
        result = self._signal_view(dict(row))
        result["write_status"] = write_status
        result["redacted"] = redacted
        return result

    def list_signals(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        subject_sku: str | None = None,
        signal_type: CompetitiveSignalType | None = None,
        eligible_only: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["s.tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("s.store_id=?")
            params.append(store_id)
        if subject_sku:
            conditions.append("s.subject_sku=?")
            params.append(subject_sku)
        if signal_type:
            conditions.append("s.signal_type=?")
            params.append(signal_type)
        if eligible_only:
            conditions.append("m.status='approved'")
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT s.*, m.status AS match_status, m.score AS match_score
                FROM competitive_signals s
                JOIN competitive_entity_matches m
                  ON m.id=s.match_id AND m.tenant_id=s.tenant_id
                WHERE {' AND '.join(conditions)}
                ORDER BY s.observed_at DESC, s.created_at DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [self._signal_view(dict(row)) for row in rows]

    def competitive_quality_overview(
        self, tenant_id: str, *, store_id: str | None = None
    ) -> dict[str, Any]:
        match_conditions = ["tenant_id=?"]
        signal_conditions = ["s.tenant_id=?"]
        params: list[Any] = [tenant_id]
        signal_params: list[Any] = [tenant_id]
        if store_id:
            match_conditions.append("store_id=?")
            signal_conditions.append("s.store_id=?")
            params.append(store_id)
            signal_params.append(store_id)
        with self.db.connect() as conn:
            match_rows = conn.execute(
                f"""
                SELECT status, recommended_status, COUNT(*) AS count
                FROM competitive_entity_matches
                WHERE {' AND '.join(match_conditions)}
                GROUP BY status, recommended_status
                """,
                tuple(params),
            ).fetchall()
            signal_row = conn.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN m.status='approved' THEN 1 ELSE 0 END) AS eligible,
                       SUM(CASE WHEN s.signal_type='review_summary' THEN 1 ELSE 0 END) AS reviews
                FROM competitive_signals s
                JOIN competitive_entity_matches m
                  ON m.id=s.match_id AND m.tenant_id=s.tenant_id
                WHERE {' AND '.join(signal_conditions)}
                """,
                tuple(signal_params),
            ).fetchone()
        status_counts = {status: 0 for status in ("pending", "approved", "rejected")}
        recommended_counts = {status: 0 for status in ("pending", "approved", "rejected")}
        for row in match_rows:
            status_counts[str(row["status"])] += int(row["count"])
            recommended_counts[str(row["recommended_status"])] += int(row["count"])
        total_matches = sum(status_counts.values())
        pending_approvable = sum(
            int(row["count"])
            for row in match_rows
            if row["status"] == "pending" and row["recommended_status"] == "approved"
        )
        return {
            "store_id": store_id,
            "matches": {
                "total": total_matches,
                "status": status_counts,
                "recommended": recommended_counts,
                "pending_approvable": pending_approvable,
                "approval_rate": self._decimal(
                    Decimal(status_counts["approved"]) / Decimal(total_matches) * 100
                ) if total_matches else "0.00",
            },
            "signals": {
                "total": int(signal_row["total"] or 0),
                "eligible": int(signal_row["eligible"] or 0),
                "review_summaries": int(signal_row["reviews"] or 0),
            },
        }

    def record(self, tenant_id: str, value: CompetitorObservationCreate) -> dict[str, Any]:
        observation_id = f"competitor-{uuid.uuid4().hex}"
        observed_at = canonical_source_time(value.observed_at)
        payload = value.model_dump(mode="json")
        payload["observed_at"] = observed_at
        payload_hash = payload_digest(payload)
        now = utc_now()
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if value.entity_match_id:
                match_row = conn.execute(
                    """
                    SELECT * FROM competitive_entity_matches
                    WHERE id=? AND tenant_id=?
                    """,
                    (value.entity_match_id, tenant_id),
                ).fetchone()
                if match_row is None:
                    raise ValueError("competitive_match_not_found")
                match = dict(match_row)
                if any(
                    str(match[field]) != str(expected)
                    for field, expected in (
                        ("store_id", value.store_id),
                        ("subject_sku", value.subject_sku),
                        ("competitor_name", value.competitor_name),
                        ("competitor_sku", value.competitor_sku),
                    )
                ):
                    raise ValueError("competitive_match_scope_mismatch")
            existing = conn.execute(
                """
                SELECT * FROM competitor_observations
                WHERE tenant_id=? AND connector_id=? AND store_id=?
                  AND subject_sku=? AND competitor_sku=? AND observed_at=?
                """,
                (
                    tenant_id, value.connector_id, value.store_id, value.subject_sku,
                    value.competitor_sku, observed_at,
                ),
            ).fetchone()
            if existing is not None:
                existing_hash = str(existing["payload_hash"] or "")
                if not existing_hash:
                    legacy = CompetitorObservationCreate(
                        connector_id=existing["connector_id"],
                        store_id=existing["store_id"],
                        subject_sku=existing["subject_sku"],
                        competitor_name=existing["competitor_name"],
                        competitor_sku=existing["competitor_sku"],
                        subject_price=existing["subject_price"],
                        competitor_price=existing["competitor_price"],
                        currency=existing["currency"],
                        source_type=existing["source_type"],
                        source_ref=existing["source_ref"],
                        is_estimate=bool(existing["is_estimate"]),
                        observed_at=existing["observed_at"],
                        source_id=existing["source_id"],
                    ).model_dump(mode="json")
                    legacy["observed_at"] = observed_at
                    existing_hash = payload_digest(legacy)
                legacy_payload = dict(payload)
                legacy_payload.pop("entity_match_id", None)
                legacy_hash = payload_digest(legacy_payload)
                comparable_hash = (
                    legacy_hash if existing_hash == legacy_hash else payload_hash
                )
                decide_write(
                    existing_source_time=str(existing["observed_at"]),
                    existing_payload_hash=existing_hash,
                    incoming_source_time=observed_at,
                    incoming_payload_hash=comparable_hash,
                )
                write_status = "idempotent"
                observation_id = str(existing["id"])
                if not existing["payload_hash"]:
                    conn.execute(
                        "UPDATE competitor_observations SET payload_hash=? WHERE id=?",
                        (payload_hash, observation_id),
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO competitor_observations(
                        id, tenant_id, connector_id, store_id, subject_sku,
                        competitor_name, competitor_sku, subject_price, competitor_price,
                        currency, rating_value, rating_scale, sales_rank, rank_scope,
                        source_type, source_ref, is_estimate, observed_at, source_id,
                        created_at, payload_hash, entity_match_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id, tenant_id, value.connector_id, value.store_id,
                        value.subject_sku, value.competitor_name, value.competitor_sku,
                        str(value.subject_price), str(value.competitor_price),
                        value.currency.upper(),
                        str(value.rating_value) if value.rating_value is not None else None,
                        str(value.rating_scale) if value.rating_scale is not None else None,
                        value.sales_rank, value.rank_scope, value.source_type, value.source_ref,
                        int(value.is_estimate), observed_at, value.source_id, now,
                        payload_hash, value.entity_match_id,
                    ),
                )
            row = conn.execute(
                """
                SELECT * FROM competitor_observations
                WHERE tenant_id=? AND connector_id=? AND store_id=?
                  AND subject_sku=? AND competitor_sku=? AND observed_at=?
                """,
                (
                    tenant_id,
                    value.connector_id,
                    value.store_id,
                    value.subject_sku,
                    value.competitor_sku,
                    observed_at,
                ),
            ).fetchone()
        result = self._view(dict(row))
        result["write_status"] = write_status
        result["alert_evaluation"] = self.evaluate_scope(
            tenant_id,
            store_id=value.store_id,
            subject_sku=value.subject_sku,
        )
        return result

    def upsert_monitor(
        self,
        tenant_id: str,
        value: CompetitiveMonitorUpsert,
        *,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM competitive_monitors
                WHERE tenant_id=? AND store_id=? AND subject_sku=?
                """,
                (tenant_id, value.store_id, value.subject_sku),
            ).fetchone()
            if existing is None:
                if value.expected_record_version != 0:
                    raise ValueError("competitive_monitor_version_conflict")
                monitor_id = f"monitor-{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO competitive_monitors(
                        id, tenant_id, store_id, subject_sku, enabled,
                        undercut_threshold_percent, price_drop_threshold_percent,
                        stale_after_hours, include_estimates, require_approved_match, created_by,
                        record_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        monitor_id,
                        tenant_id,
                        value.store_id,
                        value.subject_sku,
                        int(value.enabled),
                        self._decimal(value.undercut_threshold_percent),
                        self._decimal(value.price_drop_threshold_percent),
                        value.stale_after_hours,
                        int(value.include_estimates),
                        int(value.require_approved_match),
                        actor,
                        now,
                        now,
                    ),
                )
                write_status = "created"
            else:
                if int(existing["record_version"]) != value.expected_record_version:
                    raise ValueError("competitive_monitor_version_conflict")
                monitor_id = str(existing["id"])
                conn.execute(
                    """
                    UPDATE competitive_monitors
                    SET enabled=?, undercut_threshold_percent=?,
                        price_drop_threshold_percent=?, stale_after_hours=?,
                        include_estimates=?, require_approved_match=?,
                        record_version=record_version+1,
                        updated_at=?
                    WHERE id=? AND tenant_id=? AND record_version=?
                    """,
                    (
                        int(value.enabled),
                        self._decimal(value.undercut_threshold_percent),
                        self._decimal(value.price_drop_threshold_percent),
                        value.stale_after_hours,
                        int(value.include_estimates),
                        int(value.require_approved_match),
                        now,
                        monitor_id,
                        tenant_id,
                        value.expected_record_version,
                    ),
                )
                write_status = "updated"
            row = conn.execute(
                "SELECT * FROM competitive_monitors WHERE id=? AND tenant_id=?",
                (monitor_id, tenant_id),
            ).fetchone()
        result = self._monitor_view(dict(row))
        result["write_status"] = write_status
        result["alert_evaluation"] = self.evaluate_monitor(tenant_id, monitor_id)
        return result

    def list_monitors(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        subject_sku: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if subject_sku:
            conditions.append("subject_sku=?")
            params.append(subject_sku)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM competitive_monitors
                WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC, subject_sku
                """,
                tuple(params),
            ).fetchall()
        return [self._monitor_view(dict(row)) for row in rows]

    def evaluate_scope(
        self,
        tenant_id: str,
        *,
        store_id: str,
        subject_sku: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM competitive_monitors
                WHERE tenant_id=? AND store_id=? AND subject_sku=?
                """,
                (tenant_id, store_id, subject_sku),
            ).fetchone()
        if row is None:
            return None
        return self.evaluate_monitor(tenant_id, str(row["id"]), now=now)

    def evaluate_all(
        self,
        tenant_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        monitor_ids = [item["id"] for item in self.list_monitors(tenant_id)]
        results = [self.evaluate_monitor(tenant_id, monitor_id, now=now) for monitor_id in monitor_ids]
        return {
            "evaluated": len(results),
            "created": sum(item["created"] for item in results),
            "updated": sum(item["updated"] for item in results),
            "auto_resolved": sum(item["auto_resolved"] for item in results),
            "active_alerts": sum(item["active_alerts"] for item in results),
            "results": results,
        }

    def evaluate_all_tenants(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            tenant_ids = [
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT tenant_id FROM competitive_monitors ORDER BY tenant_id"
                ).fetchall()
            ]
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for tenant_id in tenant_ids:
            try:
                results.append(
                    {"tenant_id": tenant_id, **self.evaluate_all(tenant_id, now=now)}
                )
            except Exception as exc:
                errors.append(
                    {
                        "tenant_id": tenant_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:300],
                    }
                )
        return {
            "tenants": len(tenant_ids),
            "evaluated": sum(item["evaluated"] for item in results),
            "created": sum(item["created"] for item in results),
            "updated": sum(item["updated"] for item in results),
            "auto_resolved": sum(item["auto_resolved"] for item in results),
            "errors": errors,
            "results": results,
        }

    def evaluate_monitor(
        self,
        tenant_id: str,
        monitor_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        evaluated_at, now_value = self._evaluation_time(now)
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            monitor_row = conn.execute(
                "SELECT * FROM competitive_monitors WHERE id=? AND tenant_id=?",
                (monitor_id, tenant_id),
            ).fetchone()
            if monitor_row is None:
                raise ValueError("competitive_monitor_not_found")
            monitor = dict(monitor_row)
            observation_rows = conn.execute(
                """
                SELECT * FROM competitor_observations
                WHERE tenant_id=? AND store_id=? AND subject_sku=?
                  AND (?=1 OR is_estimate=0)
                  AND (
                    ?=0 OR EXISTS (
                        SELECT 1 FROM competitive_entity_matches m
                        WHERE m.id=competitor_observations.entity_match_id
                          AND m.tenant_id=competitor_observations.tenant_id
                          AND m.status='approved'
                    )
                  )
                ORDER BY observed_at DESC, created_at DESC
                """,
                (
                    tenant_id,
                    monitor["store_id"],
                    monitor["subject_sku"],
                    int(bool(monitor["include_estimates"])),
                    int(bool(monitor["require_approved_match"])),
                ),
            ).fetchall()
            groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for raw in observation_rows:
                row = dict(raw)
                groups.setdefault(
                    (str(row["competitor_name"]), str(row["competitor_sku"])), []
                ).append(row)

            conditions: list[dict[str, Any]] = []
            if bool(monitor["enabled"]):
                if not groups:
                    conditions.append(
                        {
                            "competitor_name": "未获取数据",
                            "competitor_sku": "__monitor__",
                            "alert_code": "data_stale",
                            "severity": "high",
                            "value": str(monitor["stale_after_hours"]),
                            "threshold_value": str(monitor["stale_after_hours"]),
                            "observation_id": None,
                            "previous_observation_id": None,
                            "evidence_key": "no-eligible-observation",
                            "details": {
                                "reason": "no_eligible_observation",
                                "include_estimates": bool(monitor["include_estimates"]),
                                "require_approved_match": bool(
                                    monitor["require_approved_match"]
                                ),
                            },
                        }
                    )
                for (competitor_name, competitor_sku), rows in groups.items():
                    latest = rows[0]
                    previous = rows[1] if len(rows) > 1 else None
                    subject_price = Decimal(str(latest["subject_price"]))
                    competitor_price = Decimal(str(latest["competitor_price"]))
                    undercut = (subject_price - competitor_price) / subject_price * Decimal("100")
                    undercut_threshold = Decimal(str(monitor["undercut_threshold_percent"]))
                    if undercut >= undercut_threshold:
                        conditions.append(
                            self._alert_condition(
                                competitor_name,
                                competitor_sku,
                                "competitor_undercut",
                                undercut,
                                undercut_threshold,
                                latest,
                                previous=None,
                            )
                        )
                    if previous is not None:
                        previous_price = Decimal(str(previous["competitor_price"]))
                        price_drop = (
                            (previous_price - competitor_price) / previous_price * Decimal("100")
                        )
                        drop_threshold = Decimal(str(monitor["price_drop_threshold_percent"]))
                        if price_drop >= drop_threshold:
                            conditions.append(
                                self._alert_condition(
                                    competitor_name,
                                    competitor_sku,
                                    "competitor_price_drop",
                                    price_drop,
                                    drop_threshold,
                                    latest,
                                    previous=previous,
                                )
                            )
                    observed_at = self._parse_time(str(latest["observed_at"]))
                    stale_hours = Decimal(str(max((now_value - observed_at).total_seconds(), 0))) / Decimal("3600")
                    stale_threshold = Decimal(str(monitor["stale_after_hours"]))
                    if stale_hours >= stale_threshold:
                        conditions.append(
                            self._alert_condition(
                                competitor_name,
                                competitor_sku,
                                "data_stale",
                                stale_hours,
                                stale_threshold,
                                latest,
                                previous=None,
                            )
                        )

            existing_rows = conn.execute(
                """
                SELECT * FROM competitive_alerts
                WHERE tenant_id=? AND monitor_id=?
                """,
                (tenant_id, monitor_id),
            ).fetchall()
            existing = {
                (str(row["competitor_sku"]), str(row["alert_code"])): dict(row)
                for row in existing_rows
            }
            active_keys: set[tuple[str, str]] = set()
            created = 0
            updated = 0
            for condition in conditions:
                key = (condition["competitor_sku"], condition["alert_code"])
                active_keys.add(key)
                current = existing.get(key)
                if current is None:
                    self._insert_alert(
                        conn, tenant_id, monitor, condition, evaluated_at
                    )
                    created += 1
                    continue
                if str(current["evidence_key"]) == condition["evidence_key"]:
                    if current["status"] != "resolved" and self._alert_definition_changed(
                        current, condition
                    ):
                        self._update_alert_definition(
                            conn, current, condition, evaluated_at
                        )
                        updated += 1
                    continue
                self._refresh_alert(conn, current, condition, evaluated_at)
                updated += 1

            auto_resolved = 0
            reason = "monitor_disabled" if not bool(monitor["enabled"]) else "condition_cleared"
            for key, current in existing.items():
                if key in active_keys or current["status"] == "resolved":
                    continue
                conn.execute(
                    """
                    UPDATE competitive_alerts
                    SET status='resolved', resolved_by='system', resolved_at=?,
                        resolution_note=?, record_version=record_version+1, updated_at=?
                    WHERE id=? AND tenant_id=?
                    """,
                    (evaluated_at, reason, evaluated_at, current["id"], tenant_id),
                )
                auto_resolved += 1
            active_alerts = conn.execute(
                """
                SELECT COUNT(*) FROM competitive_alerts
                WHERE tenant_id=? AND monitor_id=? AND status IN ('open','acknowledged')
                """,
                (tenant_id, monitor_id),
            ).fetchone()[0]
        return {
            "monitor_id": monitor_id,
            "store_id": monitor["store_id"],
            "subject_sku": monitor["subject_sku"],
            "evaluated_at": evaluated_at,
            "eligible_competitors": len(groups),
            "conditions_detected": len(conditions),
            "created": created,
            "updated": updated,
            "auto_resolved": auto_resolved,
            "active_alerts": active_alerts,
        }

    def list_alerts(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        subject_sku: str | None = None,
        status: CompetitiveAlertStatus | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if subject_sku:
            conditions.append("subject_sku=?")
            params.append(subject_sku)
        if status:
            conditions.append("status=?")
            params.append(status)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM competitive_alerts
                WHERE {' AND '.join(conditions)}
                ORDER BY CASE severity
                    WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                    WHEN 'attention' THEN 2 ELSE 1 END DESC,
                    last_detected_at DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [self._alert_view(dict(row)) for row in rows]

    def transition_alert(
        self,
        tenant_id: str,
        alert_id: str,
        value: CompetitiveAlertTransition,
        *,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM competitive_alerts WHERE id=? AND tenant_id=?",
                (alert_id, tenant_id),
            ).fetchone()
            if row is None:
                raise ValueError("competitive_alert_not_found")
            current = dict(row)
            if int(current["record_version"]) != value.expected_record_version:
                raise ValueError("competitive_alert_version_conflict")
            if value.target_status == "acknowledged":
                if current["status"] != "open":
                    raise ValueError("competitive_alert_invalid_transition")
                assignments = (
                    "status='acknowledged', acknowledged_by=?, acknowledged_at=?, "
                    "acknowledgement_note=?"
                )
            else:
                if current["status"] not in {"open", "acknowledged"}:
                    raise ValueError("competitive_alert_invalid_transition")
                assignments = "status='resolved', resolved_by=?, resolved_at=?, resolution_note=?"
            changed = conn.execute(
                f"""
                UPDATE competitive_alerts SET {assignments},
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND record_version=?
                """,
                (
                    actor,
                    now,
                    value.note,
                    now,
                    alert_id,
                    tenant_id,
                    value.expected_record_version,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("competitive_alert_version_conflict")
            changed_row = conn.execute(
                "SELECT * FROM competitive_alerts WHERE id=? AND tenant_id=?",
                (alert_id, tenant_id),
            ).fetchone()
        return self._alert_view(dict(changed_row))

    def analyze_prices(
        self,
        tenant_id: str,
        subject_sku: str,
        *,
        store_id: str | None = None,
    ) -> dict[str, Any]:
        conditions = ["tenant_id=?", "subject_sku=?"]
        params: list[Any] = [tenant_id, subject_sku]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        rows = self._query(conditions, params, limit=5001)
        history_truncated = len(rows) > 5000
        rows = rows[:5000]
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in rows:
            row = dict(raw)
            latest.setdefault((row["competitor_name"], row["competitor_sku"]), row)
        entries: list[dict[str, Any]] = []
        for row in latest.values():
            entity_match = self._observation_match(row)
            actionable = bool(entity_match and entity_match["status"] == "approved")
            subject_price = Decimal(row["subject_price"])
            competitor_price = Decimal(row["competitor_price"])
            gap = competitor_price - subject_price
            gap_percent = gap / subject_price * Decimal("100")
            if subject_price < competitor_price:
                position = "our_price_lower"
            elif subject_price > competitor_price:
                position = "our_price_higher"
            else:
                position = "same_price"
            entries.append(
                {
                    "competitor_name": row["competitor_name"],
                    "competitor_sku": row["competitor_sku"],
                    "subject_price": self._decimal(subject_price),
                    "competitor_price": self._decimal(competitor_price),
                    "gap_amount": self._decimal(gap),
                    "gap_percent": self._decimal(gap_percent),
                    "position": position,
                    "currency": row["currency"],
                    "observed_at": row["observed_at"],
                    "entity_match": entity_match,
                    "actionable": actionable,
                    "evidence": {
                        "observation_id": row["id"],
                        "connector_id": row["connector_id"],
                        "source_type": row["source_type"],
                        "source_ref": row["source_ref"],
                        "is_estimate": bool(row["is_estimate"]),
                        "entity_match_id": row.get("entity_match_id"),
                    },
                }
            )
        entries.sort(
            key=lambda item: (Decimal(item["gap_percent"]), item["competitor_name"])
        )
        history: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for raw in reversed(rows):
            row = dict(raw)
            key = (row["competitor_name"], row["competitor_sku"])
            entity_match = self._observation_match(row)
            history.setdefault(key, []).append(
                {
                    "observed_at": row["observed_at"],
                    "subject_price": self._decimal(Decimal(row["subject_price"])),
                    "competitor_price": self._decimal(Decimal(row["competitor_price"])),
                    "is_estimate": bool(row["is_estimate"]),
                    "entity_match_id": row.get("entity_match_id"),
                    "actionable": bool(
                        entity_match and entity_match["status"] == "approved"
                    ),
                }
            )
        trends = []
        for (competitor_name, competitor_sku), points in history.items():
            first = Decimal(points[0]["competitor_price"])
            latest_price = Decimal(points[-1]["competitor_price"])
            direction = "stable"
            if latest_price > first:
                direction = "up"
            elif latest_price < first:
                direction = "down"
            trends.append(
                {
                    "competitor_name": competitor_name,
                    "competitor_sku": competitor_sku,
                    "direction": direction,
                    "change_amount": self._decimal(latest_price - first),
                    "points": points,
                }
            )
        gaps = [Decimal(item["gap_percent"]) for item in entries]
        actionable_entries = [item for item in entries if item["actionable"]]
        source_types = {item["evidence"]["source_type"] for item in entries}
        monitors = self.list_monitors(
            tenant_id, store_id=store_id, subject_sku=subject_sku
        )
        alerts = [
            item
            for item in self.list_alerts(
                tenant_id, store_id=store_id, subject_sku=subject_sku
            )
            if item["status"] != "resolved"
        ]
        signals = self.list_signals(
            tenant_id,
            store_id=store_id,
            subject_sku=subject_sku,
            eligible_only=True,
            limit=200,
        )
        recommendations = self._recommendations(actionable_entries)
        if entries and not actionable_entries:
            recommendations.append(
                {
                    "type": "entity_quality",
                    "priority": "high",
                    "message": "当前价格证据尚未绑定已批准的同款匹配，只能用于质量复核，不能形成调价建议。",
                }
            )
        return {
            "subject_sku": subject_sku,
            "store_id": store_id,
            "data_as_of": max((item["observed_at"] for item in entries), default=None),
            "summary": {
                "competitors": len(entries),
                "actionable_competitors": len(actionable_entries),
                "unverified_competitors": len(entries) - len(actionable_entries),
                "history_points": len(rows),
                "history_truncated": history_truncated,
                "our_price_lower": sum(item["position"] == "our_price_lower" for item in entries),
                "our_price_higher": sum(item["position"] == "our_price_higher" for item in entries),
                "same_price": sum(item["position"] == "same_price" for item in entries),
                "average_gap_percent": self._decimal(sum(gaps) / len(gaps)) if gaps else None,
                "estimated_observations": sum(
                    item["evidence"]["is_estimate"] for item in entries
                ),
                "source_types": sorted(source_types),
            },
            "observations": entries,
            "trends": trends,
            "signals": signals,
            "content_review_insights": self._signal_insights(signals),
            "recommendations": recommendations,
            "monitors": monitors,
            "alerts": alerts,
            "guardrail": "只有绑定已批准同款匹配的证据可形成业务建议；口碑只保留聚合摘要与样本量，不保存评论者或原始评论。",
        }

    def list_observations(
        self,
        tenant_id: str,
        *,
        subject_sku: str | None = None,
        store_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if subject_sku:
            conditions.append("subject_sku=?")
            params.append(subject_sku)
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        return [self._view(dict(row)) for row in self._query(conditions, params, limit=limit)]

    def overview(
        self, tenant_id: str, *, store_id: str | None = None
    ) -> dict[str, Any]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        rows = self._query(conditions, params)
        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        source_breakdown: dict[str, int] = {}
        for raw in rows:
            row = dict(raw)
            latest.setdefault(
                (row["subject_sku"], row["competitor_name"], row["competitor_sku"]), row
            )
            source_breakdown[row["source_type"]] = source_breakdown.get(row["source_type"], 0) + 1

        positions = {"our_price_lower": 0, "our_price_higher": 0, "same_price": 0}
        alerts: list[dict[str, Any]] = []
        actionable_competitors = 0
        for row in latest.values():
            entity_match = self._observation_match(row)
            if not entity_match or entity_match["status"] != "approved":
                continue
            actionable_competitors += 1
            subject_price = Decimal(row["subject_price"])
            competitor_price = Decimal(row["competitor_price"])
            gap_percent = (competitor_price - subject_price) / subject_price * Decimal("100")
            if gap_percent > 0:
                position = "our_price_lower"
            elif gap_percent < 0:
                position = "our_price_higher"
            else:
                position = "same_price"
            positions[position] += 1
            if gap_percent <= Decimal("-5"):
                alerts.append(
                    {
                        "severity": "attention",
                        "subject_sku": row["subject_sku"],
                        "competitor_name": row["competitor_name"],
                        "gap_percent": self._decimal(gap_percent),
                        "observed_at": row["observed_at"],
                        "message": "本店价格高于该竞品至少 5%，建议结合成本、库存和活动口径复核",
                    }
                )
        alerts.sort(key=lambda item: Decimal(item["gap_percent"]))
        monitors = self.list_monitors(tenant_id, store_id=store_id)
        persistent_alerts = [
            item
            for item in self.list_alerts(tenant_id, store_id=store_id, limit=100)
            if item["status"] != "resolved"
        ]
        quality = self.competitive_quality_overview(tenant_id, store_id=store_id)
        return {
            "store_id": store_id,
            "monitored_skus": len({row["subject_sku"] for row in rows}),
            "monitor_policy_count": len(monitors),
            "competitors": len(latest),
            "actionable_competitors": actionable_competitors,
            "unverified_competitors": len(latest) - actionable_competitors,
            "observation_count": len(rows),
            "estimated_count": sum(bool(row["is_estimate"]) for row in rows),
            "positions": positions,
            "source_breakdown": source_breakdown,
            "data_as_of": max((row["observed_at"] for row in rows), default=None),
            "alerts": persistent_alerts[:10],
            "unmanaged_risks": alerts[:10] if not monitors else [],
            "quality": quality,
            "guardrail": "价格与口碑洞察必须绑定已批准同款匹配，建议保持只读并由人工复核。",
        }

    def _alert_condition(
        self,
        competitor_name: str,
        competitor_sku: str,
        alert_code: str,
        value: Decimal,
        threshold: Decimal,
        latest: dict[str, Any],
        *,
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        evidence_key = str(latest["id"])
        if previous is not None:
            evidence_key = f"{latest['id']}:{previous['id']}"
        return {
            "competitor_name": competitor_name,
            "competitor_sku": competitor_sku,
            "alert_code": alert_code,
            "severity": self._severity(value, threshold),
            "value": self._decimal(value),
            "threshold_value": self._decimal(threshold),
            "observation_id": latest["id"],
            "previous_observation_id": previous["id"] if previous else None,
            "evidence_key": evidence_key,
            "details": {
                "observed_at": latest["observed_at"],
                "subject_price": latest["subject_price"],
                "competitor_price": latest["competitor_price"],
                "currency": latest["currency"],
                "source_type": latest["source_type"],
                "source_ref": latest["source_ref"],
                "is_estimate": bool(latest["is_estimate"]),
                "entity_match_id": latest.get("entity_match_id"),
            },
        }

    @staticmethod
    def _severity(value: Decimal, threshold: Decimal) -> str:
        ratio = value / threshold
        if ratio >= Decimal("3"):
            return "critical"
        if ratio >= Decimal("2"):
            return "high"
        return "attention"

    @staticmethod
    def _insert_alert(
        conn: Any,
        tenant_id: str,
        monitor: dict[str, Any],
        condition: dict[str, Any],
        evaluated_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO competitive_alerts(
                id, tenant_id, monitor_id, store_id, subject_sku,
                competitor_name, competitor_sku, alert_code, severity, status,
                value, threshold_value, observation_id, previous_observation_id,
                evidence_key, details_json, occurrence_count, record_version,
                first_detected_at, last_detected_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)
            """,
            (
                f"compalert-{uuid.uuid4().hex}",
                tenant_id,
                monitor["id"],
                monitor["store_id"],
                monitor["subject_sku"],
                condition["competitor_name"],
                condition["competitor_sku"],
                condition["alert_code"],
                condition["severity"],
                condition["value"],
                condition["threshold_value"],
                condition["observation_id"],
                condition["previous_observation_id"],
                condition["evidence_key"],
                json.dumps(condition["details"], ensure_ascii=False, sort_keys=True),
                evaluated_at,
                evaluated_at,
                evaluated_at,
            ),
        )

    @staticmethod
    def _refresh_alert(
        conn: Any,
        current: dict[str, Any],
        condition: dict[str, Any],
        evaluated_at: str,
    ) -> None:
        conn.execute(
            """
            UPDATE competitive_alerts
            SET competitor_name=?, severity=?, status='open', value=?,
                threshold_value=?, observation_id=?, previous_observation_id=?,
                evidence_key=?, details_json=?, occurrence_count=occurrence_count+1,
                record_version=record_version+1, last_detected_at=?,
                acknowledged_by=NULL, acknowledged_at=NULL, acknowledgement_note=NULL,
                resolved_by=NULL, resolved_at=NULL, resolution_note=NULL, updated_at=?
            WHERE id=? AND tenant_id=?
            """,
            (
                condition["competitor_name"],
                condition["severity"],
                condition["value"],
                condition["threshold_value"],
                condition["observation_id"],
                condition["previous_observation_id"],
                condition["evidence_key"],
                json.dumps(condition["details"], ensure_ascii=False, sort_keys=True),
                evaluated_at,
                evaluated_at,
                current["id"],
                current["tenant_id"],
            ),
        )

    @staticmethod
    def _alert_definition_changed(
        current: dict[str, Any], condition: dict[str, Any]
    ) -> bool:
        return any(
            str(current[key]) != str(condition[key])
            for key in ("severity", "value", "threshold_value")
        )

    @staticmethod
    def _update_alert_definition(
        conn: Any,
        current: dict[str, Any],
        condition: dict[str, Any],
        evaluated_at: str,
    ) -> None:
        conn.execute(
            """
            UPDATE competitive_alerts
            SET severity=?, value=?, threshold_value=?, details_json=?,
                record_version=record_version+1, last_detected_at=?, updated_at=?
            WHERE id=? AND tenant_id=?
            """,
            (
                condition["severity"],
                condition["value"],
                condition["threshold_value"],
                json.dumps(condition["details"], ensure_ascii=False, sort_keys=True),
                evaluated_at,
                evaluated_at,
                current["id"],
                current["tenant_id"],
            ),
        )

    @staticmethod
    def _evaluation_time(value: datetime | None) -> tuple[str, datetime]:
        current = value or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("evaluation time must include timezone")
        normalized = current.astimezone(UTC)
        return normalized.isoformat(), normalized

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("stored observation time must include timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _monitor_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "store_id": row["store_id"],
            "subject_sku": row["subject_sku"],
            "enabled": bool(row["enabled"]),
            "undercut_threshold_percent": row["undercut_threshold_percent"],
            "price_drop_threshold_percent": row["price_drop_threshold_percent"],
            "stale_after_hours": row["stale_after_hours"],
            "include_estimates": bool(row["include_estimates"]),
            "require_approved_match": bool(row["require_approved_match"]),
            "created_by": row["created_by"],
            "record_version": row["record_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _alert_view(row: dict[str, Any]) -> dict[str, Any]:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError):
            details = {}
        messages = {
            "competitor_undercut": "竞品价格低于本店价格，需结合毛利、库存和活动口径复核",
            "competitor_price_drop": "竞品价格出现显著下调，需核对规格与活动后评估影响",
            "data_stale": "竞品数据已超过新鲜度阈值，决策前需刷新或人工复核",
        }
        return {
            "id": row["id"],
            "monitor_id": row["monitor_id"],
            "store_id": row["store_id"],
            "subject_sku": row["subject_sku"],
            "competitor_name": row["competitor_name"],
            "competitor_sku": row["competitor_sku"],
            "alert_code": row["alert_code"],
            "severity": row["severity"],
            "status": row["status"],
            "value": row["value"],
            "threshold_value": row["threshold_value"],
            "message": messages.get(row["alert_code"], row["alert_code"]),
            "observation_id": row["observation_id"],
            "previous_observation_id": row["previous_observation_id"],
            "details": details,
            "occurrence_count": row["occurrence_count"],
            "record_version": row["record_version"],
            "first_detected_at": row["first_detected_at"],
            "last_detected_at": row["last_detected_at"],
            "acknowledged_by": row["acknowledged_by"],
            "acknowledged_at": row["acknowledged_at"],
            "acknowledgement_note": row["acknowledgement_note"],
            "resolved_by": row["resolved_by"],
            "resolved_at": row["resolved_at"],
            "resolution_note": row["resolution_note"],
            "updated_at": row["updated_at"],
        }

    def _query(
        self,
        conditions: list[str],
        params: list[Any],
        *,
        limit: int | None = None,
    ) -> list[Any]:
        limit_clause = " LIMIT ?" if limit is not None else ""
        query_params = [*params, limit] if limit is not None else params
        with self.db.connect() as conn:
            return conn.execute(
                f"""
                SELECT o.*, m.status AS entity_match_status,
                       m.score AS entity_match_score,
                       m.recommended_status AS entity_match_recommended_status
                FROM (
                    SELECT * FROM competitor_observations
                    WHERE {' AND '.join(conditions)}
                    ORDER BY observed_at DESC, created_at DESC{limit_clause}
                ) o
                LEFT JOIN competitive_entity_matches m
                  ON m.id=o.entity_match_id AND m.tenant_id=o.tenant_id
                ORDER BY o.observed_at DESC, o.created_at DESC
                """,
                tuple(query_params),
            ).fetchall()

    @classmethod
    def _signal_insights(cls, signals: list[dict[str, Any]]) -> dict[str, Any]:
        latest: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for signal in signals:
            key = (
                str(signal["match_id"]),
                str(signal["signal_type"]),
                str(signal["entity_role"]),
                str(signal["aspect"]).casefold(),
            )
            latest.setdefault(key, signal)

        claims: list[dict[str, Any]] = []
        review_groups: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        for signal in latest.values():
            evidence = {
                "signal_id": signal["id"],
                "source_type": signal["source_type"],
                "source_ref": signal["source_ref"],
                "is_estimate": signal["is_estimate"],
                "observed_at": signal["observed_at"],
            }
            if signal["signal_type"] == "product_claim":
                claims.append(
                    {
                        "match_id": signal["match_id"],
                        "competitor_name": signal["competitor_name"],
                        "entity_role": signal["entity_role"],
                        "aspect": signal["aspect"],
                        "summary": signal["summary"],
                        "evidence": evidence,
                    }
                )
                continue
            sample_size = int(signal["sample_size"] or 0)
            positive_count = int(signal["positive_count"] or 0)
            negative_count = int(signal["negative_count"] or 0)
            review_groups.setdefault(
                (str(signal["match_id"]), str(signal["aspect"]).casefold()), {}
            )[str(signal["entity_role"])] = {
                "summary": signal["summary"],
                "sample_size": sample_size,
                "positive_count": positive_count,
                "negative_count": negative_count,
                "positive_rate": cls._decimal(
                    Decimal(positive_count) / Decimal(sample_size) * 100
                ),
                "negative_rate": cls._decimal(
                    Decimal(negative_count) / Decimal(sample_size) * 100
                ),
                "evidence": evidence,
                "aspect": signal["aspect"],
                "competitor_name": signal["competitor_name"],
            }

        reviews: list[dict[str, Any]] = []
        for (match_id, _aspect_key), roles in review_groups.items():
            subject = roles.get("subject")
            competitor = roles.get("competitor")
            comparison = "insufficient_pair"
            delta = None
            if subject and competitor:
                delta_value = Decimal(competitor["positive_rate"]) - Decimal(
                    subject["positive_rate"]
                )
                delta = cls._decimal(delta_value)
                if delta_value >= Decimal("5"):
                    comparison = "competitor_advantage"
                elif delta_value <= Decimal("-5"):
                    comparison = "subject_advantage"
                else:
                    comparison = "roughly_equal"
            exemplar = subject or competitor or {}
            reviews.append(
                {
                    "match_id": match_id,
                    "competitor_name": exemplar.get("competitor_name"),
                    "aspect": exemplar.get("aspect"),
                    "comparison": comparison,
                    "positive_rate_delta": delta,
                    "subject": subject,
                    "competitor": competitor,
                }
            )
        return {"product_claims": claims, "review_comparisons": reviews}

    @staticmethod
    def _recommendations(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
        recommendations: list[dict[str, str]] = []
        higher = [item for item in entries if item["position"] == "our_price_higher"]
        lower = [item for item in entries if item["position"] == "our_price_lower"]
        if higher:
            recommendations.append(
                {
                    "type": "price_review",
                    "priority": "high" if any(Decimal(item["gap_percent"]) <= -5 for item in higher) else "medium",
                    "message": "存在低价竞品；复核商品规格、活动、毛利和库存后再决定是否调整价格",
                }
            )
        if lower:
            recommendations.append(
                {
                    "type": "value_review",
                    "priority": "medium",
                    "message": "本店部分价格低于竞品；可复核优惠必要性并强化服务、时效或商品价值表达",
                }
            )
        if entries and all(item["evidence"]["is_estimate"] for item in entries):
            recommendations.append(
                {
                    "type": "data_quality",
                    "priority": "medium",
                    "message": "当前全部数据为估算或虚拟观察，业务决策前应补充授权来源或人工复核",
                }
            )
        return recommendations

    @classmethod
    def _assess_entity_match(
        cls, value: CompetitiveEntityMatchCreate
    ) -> dict[str, Any]:
        subject = value.subject_identity
        competitor = value.competitor_identity
        score = Decimal("0")
        matched_fields: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        missing_fields: list[str] = []

        for field, weight in (
            ("gtin", Decimal("50")),
            ("brand", Decimal("15")),
            ("model", Decimal("20")),
            ("category", Decimal("10")),
        ):
            subject_value = getattr(subject, field)
            competitor_value = getattr(competitor, field)
            left = cls._normalize_identity_value(subject_value)
            right = cls._normalize_identity_value(competitor_value)
            if not left or not right:
                missing_fields.append(field)
            elif left == right:
                score += weight
                matched_fields.append({"field": field, "weight": int(weight)})
            else:
                conflicts.append(
                    {
                        "field": field,
                        "reason": "value_mismatch",
                        "subject": str(subject_value),
                        "competitor": str(competitor_value),
                    }
                )

        title_similarity = SequenceMatcher(
            None,
            cls._normalize_identity_value(subject.title),
            cls._normalize_identity_value(competitor.title),
        ).ratio()
        title_weight = 0
        if title_similarity >= 0.85:
            title_weight = 10
        elif title_similarity >= 0.70:
            title_weight = 5
        if title_weight:
            score += Decimal(title_weight)
            matched_fields.append(
                {
                    "field": "title",
                    "weight": title_weight,
                    "similarity": round(title_similarity, 4),
                }
            )
        else:
            missing_fields.append("title_similarity")

        subject_attributes = {
            key.casefold(): (key, item) for key, item in subject.attributes.items()
        }
        competitor_attributes = {
            key.casefold(): (key, item) for key, item in competitor.attributes.items()
        }
        if value.comparison_keys:
            attribute_weight = Decimal("15") / Decimal(len(value.comparison_keys))
            for key in value.comparison_keys:
                folded = key.casefold()
                left_item = subject_attributes.get(folded)
                right_item = competitor_attributes.get(folded)
                field = f"attributes.{key}"
                if left_item is None or right_item is None:
                    missing_fields.append(field)
                    continue
                left = cls._normalize_identity_value(left_item[1])
                right = cls._normalize_identity_value(right_item[1])
                if left == right:
                    score += attribute_weight
                    matched_fields.append(
                        {"field": field, "weight": round(float(attribute_weight), 2)}
                    )
                else:
                    conflicts.append(
                        {
                            "field": field,
                            "reason": "value_mismatch",
                            "subject": left_item[1],
                            "competitor": right_item[1],
                        }
                    )

        final_score = min(int(score.quantize(Decimal("1"), rounding=ROUND_HALF_UP)), 100)
        comparison_missing = any(
            item.startswith("attributes.") for item in missing_fields
        )
        if conflicts:
            recommended_status = "rejected"
        elif final_score >= 70 and not comparison_missing:
            recommended_status = "approved"
        else:
            recommended_status = "pending"
        return {
            "score": final_score,
            "matched_fields": matched_fields,
            "conflicts": conflicts,
            "missing_fields": sorted(set(missing_fields)),
            "recommended_status": recommended_status,
        }

    @staticmethod
    def _normalize_identity_value(value: str | None) -> str:
        if value is None:
            return ""
        normalized = unicodedata.normalize("NFKC", str(value)).casefold().strip()
        return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)

    @staticmethod
    def _json_list(value: Any) -> list[Any]:
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []

    @classmethod
    def _match_view(cls, row: dict[str, Any]) -> dict[str, Any]:
        def object_value(key: str) -> dict[str, Any]:
            try:
                value = json.loads(row[key] or "{}")
            except (TypeError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}

        return {
            "id": row["id"],
            "connector_id": row["connector_id"],
            "store_id": row["store_id"],
            "subject_sku": row["subject_sku"],
            "competitor_name": row["competitor_name"],
            "competitor_sku": row["competitor_sku"],
            "subject_identity": object_value("subject_identity_json"),
            "competitor_identity": object_value("competitor_identity_json"),
            "comparison_keys": cls._json_list(row.get("comparison_keys_json")),
            "score": int(row["score"]),
            "matched_fields": cls._json_list(row.get("matched_fields_json")),
            "conflicts": cls._json_list(row.get("conflicts_json")),
            "missing_fields": cls._json_list(row.get("missing_fields_json")),
            "recommended_status": row["recommended_status"],
            "status": row["status"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "source_id": row["source_id"],
            "is_estimate": bool(row["is_estimate"]),
            "observed_at": row["observed_at"],
            "record_version": int(row["record_version"]),
            "reviewed_by": row.get("reviewed_by"),
            "reviewed_at": row.get("reviewed_at"),
            "review_note": row.get("review_note"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _match_decision_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "match_id": row["match_id"],
            "from_status": row["from_status"],
            "to_status": row["to_status"],
            "match_record_version": int(row["match_record_version"]),
            "actor": row["actor"],
            "note": row["note"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _signal_view(row: dict[str, Any]) -> dict[str, Any]:
        match_status = str(row["match_status"])
        return {
            "id": row["id"],
            "match_id": row["match_id"],
            "connector_id": row["connector_id"],
            "store_id": row["store_id"],
            "subject_sku": row["subject_sku"],
            "competitor_name": row["competitor_name"],
            "competitor_sku": row["competitor_sku"],
            "entity_role": row["entity_role"],
            "signal_type": row["signal_type"],
            "aspect": row["aspect"],
            "summary": row["summary_redacted"],
            "sample_size": row["sample_size"],
            "positive_count": row["positive_count"],
            "negative_count": row["negative_count"],
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "source_id": row["source_id"],
            "is_estimate": bool(row["is_estimate"]),
            "observed_at": row["observed_at"],
            "match_status": match_status,
            "match_score": int(row["match_score"]),
            "eligible": match_status == "approved",
            "created_at": row["created_at"],
        }

    @staticmethod
    def _decimal(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        match = (
            self._observation_match(row)
            if "entity_match_status" in row
            else self._match_brief(row["tenant_id"], row.get("entity_match_id"))
        )
        rating_value = row.get("rating_value")
        rating_scale = row.get("rating_scale")
        normalized_rating = None
        if rating_value is not None and rating_scale is not None:
            normalized_rating = self._decimal(
                Decimal(str(rating_value)) / Decimal(str(rating_scale)) * Decimal("5")
            )
        return {
            "id": row["id"],
            "connector_id": row["connector_id"],
            "store_id": row["store_id"],
            "subject_sku": row["subject_sku"],
            "competitor_name": row["competitor_name"],
            "competitor_sku": row["competitor_sku"],
            "subject_price": row["subject_price"],
            "competitor_price": row["competitor_price"],
            "currency": row["currency"],
            "rating_value": rating_value,
            "rating_scale": rating_scale,
            "normalized_rating": normalized_rating,
            "sales_rank": row.get("sales_rank"),
            "rank_scope": row.get("rank_scope"),
            "source_type": row["source_type"],
            "source_ref": row["source_ref"],
            "is_estimate": bool(row["is_estimate"]),
            "observed_at": row["observed_at"],
            "source_id": row["source_id"],
            "entity_match": match,
            "actionable": bool(match and match["status"] == "approved"),
        }

    @staticmethod
    def _observation_match(row: dict[str, Any]) -> dict[str, Any] | None:
        match_id = row.get("entity_match_id")
        if not match_id:
            return None
        status = row.get("entity_match_status")
        if status is None:
            return {
                "id": match_id,
                "status": "missing",
                "score": None,
                "recommended_status": None,
            }
        return {
            "id": match_id,
            "status": status,
            "score": int(row["entity_match_score"]),
            "recommended_status": row["entity_match_recommended_status"],
        }

    def _match_brief(
        self, tenant_id: str, match_id: str | None
    ) -> dict[str, Any] | None:
        if not match_id:
            return None
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT id, status, score, recommended_status
                FROM competitive_entity_matches WHERE id=? AND tenant_id=?
                """,
                (match_id, tenant_id),
            ).fetchone()
        if row is None:
            return {
                "id": match_id,
                "status": "missing",
                "score": None,
                "recommended_status": None,
            }
        return {
            "id": row["id"],
            "status": row["status"],
            "score": int(row["score"]),
            "recommended_status": row["recommended_status"],
        }
