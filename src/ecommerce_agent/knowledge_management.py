from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .database import Database, utc_now
from .rag import KnowledgeBase


class KnowledgeLifecycleError(ValueError):
    pass


KnowledgeLayer = Literal["platform", "industry", "store", "product", "evolution"]


class KnowledgeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=80)
    intent: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    question: str = Field(min_length=2, max_length=500)
    answer: str = Field(min_length=2, max_length=2000)
    keywords: str = Field(default="", max_length=500)
    risk_level: Literal["low", "medium", "high"] = "low"
    source: str = Field(min_length=3, max_length=500)
    layer: KnowledgeLayer
    store_id: str | None = Field(default=None, max_length=128)
    sku_id: str | None = Field(default=None, max_length=128)


class KnowledgeReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    question: str | None = Field(default=None, min_length=2, max_length=500)
    answer: str | None = Field(default=None, min_length=2, max_length=2000)
    keywords: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, min_length=3, max_length=500)


class KnowledgeTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=1000)


class KnowledgeManagementService:
    def __init__(self, db: Database, knowledge: KnowledgeBase):
        self.db = db
        self.knowledge = knowledge

    def list_items(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        layer: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if status:
            conditions.append("status=?")
            params.append(status)
        if layer:
            conditions.append("layer=?")
            params.append(layer)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, knowledge_key, category, intent, question, answer, keywords,
                       risk_level, source, version, status, review_status, layer,
                       store_id, sku_id, approved_by, checksum, effective_from,
                       effective_to, record_version, created_at, updated_at
                FROM knowledge WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC, version DESC LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_item(self, tenant_id: str, item_id: str) -> dict[str, Any] | None:
        items = self._rows_for_ids(tenant_id, [item_id])
        return items[0] if items else None

    def create(
        self, tenant_id: str, request: KnowledgeCreateRequest, actor: str
    ) -> dict[str, Any]:
        self._validate_scope(request.layer, request.store_id, request.sku_id)
        item_id = self.knowledge.add_document(
            **request.model_dump(),
            tenant_id=tenant_id,
            knowledge_key=f"knowledge-{uuid.uuid4().hex}",
            status="candidate",
            review_status="draft",
        )
        self.db.audit("knowledge.draft_created", actor, item_id, request.model_dump(), tenant_id)
        return self._require(tenant_id, item_id)

    def revise(
        self, tenant_id: str, item_id: str, request: KnowledgeReviseRequest, actor: str
    ) -> dict[str, Any]:
        current = self._require(tenant_id, item_id)
        if current["record_version"] != request.expected_record_version:
            raise KnowledgeLifecycleError("knowledge version conflict")
        with self.db.connect() as conn:
            next_version = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM knowledge "
                    "WHERE tenant_id=? AND knowledge_key=?",
                    (tenant_id, current["knowledge_key"]),
                ).fetchone()[0]
            )
        values = request.model_dump(exclude={"expected_record_version"}, exclude_none=True)
        new_id = self.knowledge.add_document(
            category=current["category"],
            intent=current["intent"],
            question=values.get("question", current["question"]),
            answer=values.get("answer", current["answer"]),
            keywords=values.get("keywords", current["keywords"]),
            risk_level=current["risk_level"],
            source=values.get("source", current["source"]),
            version=next_version,
            status="candidate",
            tenant_id=tenant_id,
            knowledge_key=current["knowledge_key"],
            layer=current["layer"],
            store_id=current["store_id"],
            sku_id=current["sku_id"],
            review_status="draft",
        )
        self.db.audit(
            "knowledge.version_created",
            actor,
            new_id,
            {"knowledge_key": current["knowledge_key"], "version": next_version},
            tenant_id,
        )
        return self._require(tenant_id, new_id)

    def evaluate(
        self, tenant_id: str, item_id: str, request: KnowledgeTransitionRequest, actor: str
    ) -> dict[str, Any]:
        item = self._transition(
            tenant_id,
            item_id,
            request.expected_record_version,
            from_status="candidate",
            from_review="draft",
            to_status="candidate",
            to_review="evaluated",
        )
        self.db.audit(
            "knowledge.evaluated", actor, item_id, {"note": request.note}, tenant_id
        )
        return item

    def approve(
        self, tenant_id: str, item_id: str, request: KnowledgeTransitionRequest, actor: str
    ) -> dict[str, Any]:
        current = self._require(tenant_id, item_id)
        if current["status"] != "candidate" or current["review_status"] != "evaluated":
            raise KnowledgeLifecycleError("knowledge must be evaluated before approval")
        if current["record_version"] != request.expected_record_version:
            raise KnowledgeLifecycleError("knowledge version conflict")
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE knowledge
                SET status='retired', effective_to=?, record_version=record_version+1, updated_at=?
                WHERE tenant_id=? AND knowledge_key=? AND status='active' AND id<>?
                """,
                (now, now, tenant_id, current["knowledge_key"], item_id),
            )
            cursor = conn.execute(
                """
                UPDATE knowledge
                SET status='active', review_status='approved', approved_by=?,
                    effective_from=?, effective_to=NULL, record_version=record_version+1,
                    updated_at=?
                WHERE id=? AND tenant_id=? AND record_version=? AND status='candidate'
                """,
                (actor, now, now, item_id, tenant_id, request.expected_record_version),
            )
            if cursor.rowcount != 1:
                raise KnowledgeLifecycleError("knowledge version conflict")
        self.db.audit("knowledge.activated", actor, item_id, {"note": request.note}, tenant_id)
        return self._require(tenant_id, item_id)

    def retire(
        self, tenant_id: str, item_id: str, request: KnowledgeTransitionRequest, actor: str
    ) -> dict[str, Any]:
        item = self._transition(
            tenant_id,
            item_id,
            request.expected_record_version,
            from_status="active",
            from_review="approved",
            to_status="retired",
            to_review="approved",
            close=True,
        )
        self.db.audit("knowledge.retired", actor, item_id, {"note": request.note}, tenant_id)
        return item

    def rollback(
        self, tenant_id: str, item_id: str, request: KnowledgeTransitionRequest, actor: str
    ) -> dict[str, Any]:
        target = self._require(tenant_id, item_id)
        if target["status"] != "retired" or target["review_status"] != "approved":
            raise KnowledgeLifecycleError("rollback target must be an approved retired version")
        if target["record_version"] != request.expected_record_version:
            raise KnowledgeLifecycleError("knowledge version conflict")
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE knowledge SET status='retired', effective_to=?,
                    record_version=record_version+1, updated_at=?
                WHERE tenant_id=? AND knowledge_key=? AND status='active'
                """,
                (now, now, tenant_id, target["knowledge_key"]),
            )
            cursor = conn.execute(
                """
                UPDATE knowledge SET status='active', effective_from=?, effective_to=NULL,
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND record_version=? AND status='retired'
                """,
                (now, now, item_id, tenant_id, request.expected_record_version),
            )
            if cursor.rowcount != 1:
                raise KnowledgeLifecycleError("knowledge version conflict")
        self.db.audit("knowledge.rolled_back", actor, item_id, {"note": request.note}, tenant_id)
        return self._require(tenant_id, item_id)

    def _transition(
        self,
        tenant_id: str,
        item_id: str,
        expected_version: int,
        *,
        from_status: str,
        from_review: str,
        to_status: str,
        to_review: str,
        close: bool = False,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE knowledge SET status=?, review_status=?, effective_to=?,
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND record_version=?
                  AND status=? AND review_status=?
                """,
                (
                    to_status,
                    to_review,
                    now if close else None,
                    now,
                    item_id,
                    tenant_id,
                    expected_version,
                    from_status,
                    from_review,
                ),
            )
            if cursor.rowcount != 1:
                raise KnowledgeLifecycleError("invalid knowledge transition or version conflict")
        return self._require(tenant_id, item_id)

    def _rows_for_ids(self, tenant_id: str, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, knowledge_key, category, intent, question, answer, keywords,
                       risk_level, source, version, status, review_status, layer,
                       store_id, sku_id, approved_by, checksum, effective_from,
                       effective_to, record_version, created_at, updated_at
                FROM knowledge WHERE tenant_id=? AND id IN ({placeholders})
                """,
                (tenant_id, *ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def _require(self, tenant_id: str, item_id: str) -> dict[str, Any]:
        item = self.get_item(tenant_id, item_id)
        if item is None:
            raise KnowledgeLifecycleError("knowledge item not found")
        return item

    @staticmethod
    def _validate_scope(layer: str, store_id: str | None, sku_id: str | None) -> None:
        if layer == "store" and not store_id:
            raise KnowledgeLifecycleError("store layer requires store_id")
        if layer == "product" and (not store_id or not sku_id):
            raise KnowledgeLifecycleError("product layer requires store_id and sku_id")
        if layer in {"platform", "industry"} and (store_id or sku_id):
            raise KnowledgeLifecycleError("global layers cannot have store_id or sku_id")
