from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .database import Database, utc_now


STORE_BUSINESS_CALENDAR_POLICY_VERSION = "store-business-calendar-v1"


class StoreBusinessCalendarError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class StoreBusinessCalendarUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    timezone: str = Field(min_length=1, max_length=128)
    effective_from: datetime
    changed_by: str = Field(min_length=1, max_length=128)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            return ZoneInfo(value).key
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("business_timezone_invalid") from exc

    @field_validator("effective_from")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value


class StoreBusinessCalendarService:
    def __init__(self, db: Database):
        self.db = db

    def upsert_calendar(
        self,
        tenant_id: str,
        value: StoreBusinessCalendarUpsert,
    ) -> dict[str, Any]:
        from .business.source_versioning import payload_digest

        effective_from = value.effective_from.astimezone(UTC).isoformat()
        payload_hash = payload_digest(
            {
                "store_id": value.store_id,
                "timezone": value.timezone,
                "effective_from": effective_from,
                "policy_version": STORE_BUSINESS_CALENDAR_POLICY_VERSION,
            }
        )
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM store_business_calendars
                WHERE tenant_id=? AND store_id=? AND effective_from=?
                """,
                (tenant_id, value.store_id, effective_from),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise StoreBusinessCalendarError(
                        "business_calendar_effective_time_conflict"
                    )
                calendar_id = str(existing["calendar_id"])
                write_status = "idempotent"
            else:
                latest = conn.execute(
                    """
                    SELECT COALESCE(MAX(record_version), 0)
                    FROM store_business_calendars
                    WHERE tenant_id=? AND store_id=?
                    """,
                    (tenant_id, value.store_id),
                ).fetchone()
                record_version = int(latest[0]) + 1
                calendar_id = f"store-calendar-{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO store_business_calendars(
                        calendar_id, tenant_id, store_id, timezone,
                        record_version, effective_from, changed_by,
                        policy_version, payload_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        calendar_id,
                        tenant_id,
                        value.store_id,
                        value.timezone,
                        record_version,
                        effective_from,
                        value.changed_by,
                        STORE_BUSINESS_CALENDAR_POLICY_VERSION,
                        payload_hash,
                        utc_now(),
                    ),
                )
        result = self.get_by_id(tenant_id, calendar_id)
        result["write_status"] = write_status
        return result

    def get_by_id(self, tenant_id: str, calendar_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM store_business_calendars
                WHERE tenant_id=? AND calendar_id=?
                """,
                (tenant_id, calendar_id),
            ).fetchone()
        if row is None:
            raise StoreBusinessCalendarError("store_business_calendar_not_found")
        return self._view(dict(row))

    def get_effective(
        self,
        tenant_id: str,
        store_id: str,
        *,
        at: datetime,
    ) -> dict[str, Any]:
        if at.tzinfo is None or at.utcoffset() is None:
            raise StoreBusinessCalendarError("timezone_required")
        effective_at = at.astimezone(UTC).isoformat()
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM store_business_calendars
                WHERE tenant_id=? AND store_id=? AND effective_from<=?
                ORDER BY effective_from DESC, record_version DESC
                LIMIT 1
                """,
                (tenant_id, store_id, effective_at),
            ).fetchone()
        if row is None:
            raise StoreBusinessCalendarError("store_business_calendar_not_found")
        return self._view(dict(row))

    def get_latest(self, tenant_id: str, store_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM store_business_calendars
                WHERE tenant_id=? AND store_id=?
                ORDER BY record_version DESC
                LIMIT 1
                """,
                (tenant_id, store_id),
            ).fetchone()
        if row is None:
            raise StoreBusinessCalendarError("store_business_calendar_not_found")
        return self._view(dict(row))

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "calendar_id": row["calendar_id"],
            "store_id": row["store_id"],
            "timezone": row["timezone"],
            "record_version": row["record_version"],
            "effective_from": row["effective_from"],
            "changed_by": row["changed_by"],
            "policy_version": row["policy_version"],
            "payload_hash": row["payload_hash"],
            "created_at": row["created_at"],
        }
