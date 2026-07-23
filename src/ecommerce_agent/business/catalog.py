from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..database import Database, utc_now
from .source_versioning import canonical_source_time, decide_write, payload_digest


CatalogStatus = Literal["draft", "active", "inactive", "deleted"]
JsonScalar = str | int | float | bool | None


class CatalogItemUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(min_length=1, max_length=128)
    store_id: str = Field(min_length=1, max_length=128)
    item_id: str = Field(min_length=1, max_length=128)
    sku_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    status: CatalogStatus
    sale_price: Decimal = Field(ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    attributes: dict[str, JsonScalar] = Field(default_factory=dict)
    source_updated_at: datetime
    source_id: str | None = Field(default=None, max_length=256)

    @field_validator("source_updated_at")
    @classmethod
    def require_aware_source_time(cls, value: datetime) -> datetime:
        canonical_source_time(value)
        return value


class CatalogService:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, tenant_id: str, value: CatalogItemUpsert) -> dict[str, Any]:
        payload = value.model_dump(mode="json")
        source_time = canonical_source_time(value.source_updated_at)
        payload["source_updated_at"] = source_time
        payload_hash = payload_digest(payload)
        now = utc_now()
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT id, source_updated_at, payload_hash, version
                FROM catalog_items
                WHERE tenant_id=? AND connector_id=? AND store_id=? AND sku_id=?
                """,
                (tenant_id, value.connector_id, value.store_id, value.sku_id),
            ).fetchone()
            if existing is not None:
                decision = decide_write(
                    existing_source_time=str(existing["source_updated_at"]),
                    existing_payload_hash=str(existing["payload_hash"]),
                    incoming_source_time=source_time,
                    incoming_payload_hash=payload_hash,
                )
                if decision == "idempotent":
                    write_status = "idempotent"
                    item_id = str(existing["id"])
                else:
                    item_id = str(existing["id"])
            else:
                item_id = f"catalog-{uuid.uuid4().hex}"

            if write_status == "applied":
                version = int(existing["version"]) + 1 if existing else 1
                conn.execute(
                    """
                    INSERT INTO catalog_items(
                        id, tenant_id, connector_id, store_id, item_id, sku_id,
                        title, status, sale_price, currency, attributes_json,
                        source_id, source_updated_at, payload_hash, version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, connector_id, store_id, sku_id)
                    DO UPDATE SET
                        item_id=excluded.item_id, title=excluded.title,
                        status=excluded.status, sale_price=excluded.sale_price,
                        currency=excluded.currency,
                        attributes_json=excluded.attributes_json,
                        source_id=excluded.source_id,
                        source_updated_at=excluded.source_updated_at,
                        payload_hash=excluded.payload_hash,
                        version=excluded.version, updated_at=excluded.updated_at
                    """,
                    (
                        item_id,
                        tenant_id,
                        value.connector_id,
                        value.store_id,
                        value.item_id,
                        value.sku_id,
                        value.title,
                        value.status,
                        str(value.sale_price),
                        value.currency,
                        json.dumps(value.attributes, ensure_ascii=False, sort_keys=True),
                        value.source_id,
                        source_time,
                        payload_hash,
                        version,
                        now,
                        now,
                    ),
                )
        result = self._row_by_id(tenant_id, item_id)
        result["write_status"] = write_status
        return result

    def list_items(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        sku_id: str | None = None,
        status: CatalogStatus | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if sku_id:
            conditions.append("sku_id=?")
            params.append(sku_id)
        if status:
            conditions.append("status=?")
            params.append(status)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM catalog_items
                WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._view(dict(row)) for row in rows]

    def _row_by_id(self, tenant_id: str, item_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM catalog_items WHERE id=? AND tenant_id=?",
                (item_id, tenant_id),
            ).fetchone()
        if row is None:
            raise ValueError("catalog_item_not_found")
        return self._view(dict(row))

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "connector_id": row["connector_id"],
            "store_id": row["store_id"],
            "item_id": row["item_id"],
            "sku_id": row["sku_id"],
            "title": row["title"],
            "status": row["status"],
            "sale_price": row["sale_price"],
            "currency": row["currency"],
            "attributes": json.loads(row["attributes_json"]),
            "source_id": row["source_id"],
            "source_updated_at": row["source_updated_at"],
            "data_quality": "traceable" if row["source_id"] else "source_id_missing",
            "version": row["version"],
            "updated_at": row["updated_at"],
        }
