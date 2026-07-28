from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..database import Database, utc_now
from ..text_utils import search_terms
from .source_versioning import canonical_source_time, decide_write, payload_digest


CatalogStatus = Literal["draft", "active", "inactive", "deleted"]
JsonScalar = str | int | float | bool | None

# A single-character token ("5", "l") is never specific enough on its own to claim
# a product match, so a candidate needs at least one two-character term.
MIN_SIGNIFICANT_TERM_WEIGHT = 2
TERM_WEIGHT_CAP = 4
MAX_QUERY_TERMS = 32


def _term_weight(term: str) -> int:
    return min(len(term), TERM_WEIGHT_CAP)


def _weight_sum(terms: list[str] | set[str]) -> int:
    return sum(_term_weight(term) for term in terms)


def query_terms(keyword: str) -> list[str]:
    """Customer wording reduced to the terms worth matching against the catalog."""

    return search_terms(keyword)[:MAX_QUERY_TERMS]


def _escape_like(term: str) -> str:
    escaped = term.replace("\\", "\\\\")
    for wildcard in ("%", "_"):
        escaped = escaped.replace(wildcard, f"\\{wildcard}")
    return escaped


def _item_terms(item: dict[str, Any]) -> set[str]:
    parts = [str(item["title"]), str(item["sku_id"]), str(item["item_id"])]
    for _key, value in sorted(item["attributes"].items()):
        if value is None or isinstance(value, bool):
            continue
        parts.append(str(value))
    return set(search_terms(" ".join(parts)))


def match_score(terms: list[str], item: dict[str, Any]) -> tuple[float, list[str]]:
    """Score customer wording against one catalog item.

    Same lexical shape as knowledge retrieval: matched weight over the geometric
    mean of query and item weight, so noise words in a full sentence lower every
    candidate equally and never change the ranking.
    """

    item_terms = _item_terms(item)
    matched = [term for term in terms if term in item_terms]
    if not matched:
        return 0.0, []
    if max(_term_weight(term) for term in matched) < MIN_SIGNIFICANT_TERM_WEIGHT:
        return 0.0, []
    denominator = math.sqrt(_weight_sum(terms) * _weight_sum(item_terms))
    if denominator <= 0:
        return 0.0, []
    return round(min(1.0, _weight_sum(matched) / denominator), 4), matched


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

    def search_items(
        self,
        tenant_id: str,
        *,
        keyword: str,
        store_id: str | None = None,
        status: CatalogStatus | None = None,
        limit: int = 5,
        candidate_limit: int = 200,
        min_relative_score: float = 0.35,
    ) -> list[dict[str, Any]]:
        """Resolve customer wording to real SKUs.

        Customers say "空气炸锅 5L", never "QC-AF5-WHITE", so the identifier has to be
        looked up here instead of being demanded from the customer. Matching covers the
        title, the connector ids and the attribute values (brand, category, model,
        colour, capacity). Deleted items are never surfaced.

        `min_relative_score` drops candidates that only share a weak signal with the
        best match, such as every item of the same brand, so an ambiguous result means
        the customer really has to choose.
        """

        terms = query_terms(keyword)
        if not terms:
            return []
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id:
            conditions.append("store_id=?")
            params.append(store_id)
        if status:
            conditions.append("status=?")
            params.append(status)
        else:
            conditions.append("status<>'deleted'")
        fragments = []
        for term in terms:
            fragments.append(
                "(title LIKE ? ESCAPE '\\' OR sku_id LIKE ? ESCAPE '\\' "
                "OR item_id LIKE ? ESCAPE '\\' OR attributes_json LIKE ? ESCAPE '\\')"
            )
            pattern = f"%{_escape_like(term)}%"
            params.extend([pattern, pattern, pattern, pattern])
        conditions.append(f"({' OR '.join(fragments)})")
        params.append(candidate_limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM catalog_items
                WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC, sku_id ASC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        ranked: list[dict[str, Any]] = []
        for row in rows:
            item = self._view(dict(row))
            score, matched = match_score(terms, item)
            if score <= 0:
                continue
            ranked.append({**item, "match_score": score, "matched_terms": matched})
        if not ranked:
            return []
        ranked.sort(key=lambda item: (-item["match_score"], str(item["sku_id"])))
        threshold = ranked[0]["match_score"] * min_relative_score
        relevant = [item for item in ranked if item["match_score"] >= threshold]
        return relevant[: max(1, limit)]

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
