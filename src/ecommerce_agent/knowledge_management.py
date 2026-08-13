from __future__ import annotations

import sqlite3
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


class KnowledgeRolloutBeginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    traffic_percentage: int = Field(ge=1, le=100)
    note: str | None = Field(default=None, max_length=1000)


class KnowledgeRolloutUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    traffic_percentage: int = Field(ge=1, le=100)


class KnowledgeRolloutTransitionRequest(BaseModel):
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
        self,
        tenant_id: str,
        request: KnowledgeCreateRequest,
        actor: str,
        knowledge_key: str | None = None,
    ) -> dict[str, Any]:
        """创建知识草稿（candidate/draft）。

        knowledge_key: 可选。默认 f"knowledge-{uuid}"（向后兼容）；
        Wiki 编辑传 f"kg-{item_id}" 以覆盖资产层同名词条（编辑即时生效闭环）。
        """
        self._validate_scope(request.layer, request.store_id, request.sku_id)
        # P3 竞态：插入入 _write_lock（RLock 可重入，add_document 内部持锁不冲突）。
        # 注意：不预检同 key active——create 只建 candidate，Wiki 编辑已生效词条
        # 走此路径是合法语义（新候选版本），approve 时先 retire 旧 active 再由
        # v31 唯一索引兜底单 active（终审发现：预检曾把 Wiki 二次编辑 100% 拦死）。
        with self.db._write_lock:
            item_id = self.knowledge.add_document(
                **request.model_dump(),
                tenant_id=tenant_id,
                knowledge_key=knowledge_key or f"knowledge-{uuid.uuid4().hex}",
                status="candidate",
                review_status="draft",
            )
        self.db.audit("knowledge.draft_created", actor, item_id, request.model_dump(), tenant_id)
        return self._require(tenant_id, item_id)

    def revise(
        self, tenant_id: str, item_id: str, request: KnowledgeReviseRequest, actor: str
    ) -> dict[str, Any]:
        # P3 竞态：读当前 + record_version 校验 + MAX(version)+1 分配 + 插入
        # 整体入 _write_lock，防并发 revise 产生重复版本号（RLock 可重入）
        with self.db._write_lock:
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
            # 终审修复（P1-2）：retire 旧 active 时含全局行（tenant_id IS NULL），
            # 否则 NULL 租户的旧 active 不被退，与 COALESCE 唯一索引语义脱节
            conn.execute(
                """
                UPDATE knowledge
                SET status='retired', effective_to=?, record_version=record_version+1, updated_at=?
                WHERE (tenant_id=? OR tenant_id IS NULL) AND knowledge_key=? AND status='active' AND id<>?
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
                WHERE (tenant_id=? OR tenant_id IS NULL) AND knowledge_key=? AND status='active'
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

    def begin_rollout(
        self,
        tenant_id: str,
        item_id: str,
        request: KnowledgeRolloutBeginRequest,
        actor: str,
    ) -> dict[str, Any]:
        candidate = self._require(tenant_id, item_id)
        if candidate["status"] != "candidate" or candidate["review_status"] != "evaluated":
            raise KnowledgeLifecycleError(
                "knowledge rollout requires an evaluated candidate version"
            )
        if candidate["record_version"] != request.expected_record_version:
            raise KnowledgeLifecycleError("knowledge version conflict")
        now = utc_now()
        rollout_id = f"rollout-{uuid.uuid4().hex}"
        with self.db._write_lock, self.db.connect() as conn:
            baseline = conn.execute(
                "SELECT id FROM knowledge WHERE (tenant_id=? OR tenant_id IS NULL) "
                "AND knowledge_key=? AND status='active'",
                (tenant_id, candidate["knowledge_key"]),
            ).fetchone()
            try:
                conn.execute(
                    """
                    INSERT INTO staged_rollouts(
                        id, tenant_id, subject_type, subject_key, candidate_id,
                        baseline_id, traffic_percentage, rollout_salt, status, note,
                        record_version, created_by, completed_by, created_at,
                        updated_at, completed_at
                    ) VALUES (?, ?, 'knowledge', ?, ?, ?, ?, ?, 'active', ?, 1, ?,
                              NULL, ?, ?, NULL)
                    """,
                    (
                        rollout_id,
                        tenant_id,
                        candidate["knowledge_key"],
                        item_id,
                        baseline["id"] if baseline else None,
                        request.traffic_percentage,
                        uuid.uuid4().hex,
                        request.note,
                        actor,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise KnowledgeLifecycleError(
                    "knowledge key already has an active rollout"
                ) from exc
        self.db.audit(
            "knowledge.rollout_started",
            actor,
            rollout_id,
            {
                "knowledge_key": candidate["knowledge_key"],
                "candidate_id": item_id,
                "traffic_percentage": request.traffic_percentage,
            },
            tenant_id,
        )
        return self.get_rollout(tenant_id, rollout_id)

    def update_rollout(
        self,
        tenant_id: str,
        rollout_id: str,
        request: KnowledgeRolloutUpdateRequest,
        actor: str,
    ) -> dict[str, Any]:
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE staged_rollouts
                SET traffic_percentage=?, record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND subject_type='knowledge'
                  AND status='active' AND record_version=?
                """,
                (
                    request.traffic_percentage,
                    utc_now(),
                    rollout_id,
                    tenant_id,
                    request.expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                raise KnowledgeLifecycleError(
                    "knowledge rollout transition or version conflict"
                )
        self.db.audit(
            "knowledge.rollout_adjusted",
            actor,
            rollout_id,
            {"traffic_percentage": request.traffic_percentage},
            tenant_id,
        )
        return self.get_rollout(tenant_id, rollout_id)

    def complete_rollout(
        self,
        tenant_id: str,
        rollout_id: str,
        request: KnowledgeRolloutTransitionRequest,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            rollout = conn.execute(
                """
                SELECT * FROM staged_rollouts
                WHERE id=? AND tenant_id=? AND subject_type='knowledge'
                """,
                (rollout_id, tenant_id),
            ).fetchone()
            if rollout is None:
                raise KnowledgeLifecycleError("knowledge rollout not found")
            if (
                rollout["status"] != "active"
                or rollout["record_version"] != request.expected_record_version
            ):
                raise KnowledgeLifecycleError(
                    "knowledge rollout transition or version conflict"
                )
            candidate = conn.execute(
                "SELECT * FROM knowledge WHERE id=? AND tenant_id=? AND status='candidate'",
                (rollout["candidate_id"], tenant_id),
            ).fetchone()
            if candidate is None:
                raise KnowledgeLifecycleError(
                    "rollout candidate is no longer a candidate version"
                )
            conn.execute(
                """
                UPDATE knowledge
                SET status='retired', effective_to=?, record_version=record_version+1,
                    updated_at=?
                WHERE (tenant_id=? OR tenant_id IS NULL) AND knowledge_key=? AND status='active' AND id<>?
                """,
                (now, now, tenant_id, rollout["subject_key"], rollout["candidate_id"]),
            )
            conn.execute(
                """
                UPDATE knowledge
                SET status='active', review_status='approved', approved_by=?,
                    effective_from=?, effective_to=NULL,
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=?
                """,
                (actor, now, now, rollout["candidate_id"], tenant_id),
            )
            conn.execute(
                """
                UPDATE staged_rollouts
                SET status='completed', completed_by=?, completed_at=?,
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=?
                """,
                (actor, now, now, rollout_id, tenant_id),
            )
        self.db.audit(
            "knowledge.rollout_completed",
            actor,
            rollout_id,
            {"candidate_id": rollout["candidate_id"], "note": request.note},
            tenant_id,
        )
        return self.get_rollout(tenant_id, rollout_id)

    def rollback_rollout(
        self,
        tenant_id: str,
        rollout_id: str,
        request: KnowledgeRolloutTransitionRequest,
        actor: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE staged_rollouts
                SET status='rolled_back', completed_by=?, completed_at=?, note=?,
                    record_version=record_version+1, updated_at=?
                WHERE id=? AND tenant_id=? AND subject_type='knowledge'
                  AND status='active' AND record_version=?
                """,
                (
                    actor,
                    now,
                    request.note,
                    now,
                    rollout_id,
                    tenant_id,
                    request.expected_record_version,
                ),
            )
            if cursor.rowcount != 1:
                raise KnowledgeLifecycleError(
                    "knowledge rollout transition or version conflict"
                )
        self.db.audit(
            "knowledge.rollout_rolled_back",
            actor,
            rollout_id,
            {"note": request.note},
            tenant_id,
        )
        return self.get_rollout(tenant_id, rollout_id)

    def list_rollouts(
        self, tenant_id: str, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id=?", "subject_type='knowledge'"]
        params: list[Any] = [tenant_id]
        if status:
            conditions.append("status=?")
            params.append(status)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM staged_rollouts WHERE {' AND '.join(conditions)}
                ORDER BY updated_at DESC LIMIT ?
                """,
                (*params, max(1, min(500, limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_rollout(self, tenant_id: str, rollout_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM staged_rollouts
                WHERE id=? AND tenant_id=? AND subject_type='knowledge'
                """,
                (rollout_id, tenant_id),
            ).fetchone()
        if row is None:
            raise KnowledgeLifecycleError("knowledge rollout not found")
        return dict(row)

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
