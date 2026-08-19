"""M9-R WP3 建议持久化读写服务：Recommendation / AuditRecord 落库到 v36 两张表。

边界声明：
- 薄 service：业务逻辑（状态机/校验/B3）在内存模块，本服务只做序列化 + 落库
  （仿 TrafficLabService._create_analysis_run「引擎算好结果 → service 持久化」）。
- 写路径：create（强制 DRAFT + validate_full_recommendation，复用 validation.py
  写屏障语义）、record_transition（同事务 UPDATE state + INSERT audit）。
- 幂等：create 同键同内容 -> idempotent 复用；同键异内容 -> recommendation_conflict
  （不静默覆盖）。record_transition 同 (action, actor, occurred_at) 重放 -> idempotent。
- 失败暴露：缺失/非法转换/时区/冲突 -> 抛 RecommendationError / ValueError。
- 不触发任何平台写（B2/B4）：本服务只写 v36 内部表。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ecommerce_agent.business.source_versioning import canonical_source_time, payload_digest
from ecommerce_agent.database import Database

from .schemas import Recommendation, RecommendationState, RecommendationType, TargetObject
from .state_machine import AuditRecord, StateMachine, TransitionAction
from .validation import validate_full_recommendation


class RecommendationError(ValueError):
    """服务层错误，带 code 便于上层映射。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(value: Any) -> Any:
    return json.loads(str(value))


def _tenant_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise RecommendationError("invalid_tenant_id")
    return value.strip()


def _bounded_limit(value: int, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if not 1 <= value <= maximum:
        raise RecommendationError("recommendation_limit_invalid")
    return value


def _recommendation_content_payload(recommendation: Recommendation) -> dict[str, Any]:
    """内容指纹（落库后除身份/作用域/可变状态/生命周期外的全部内容列）。

    排除：recommendation_id（查键）、tenant_id（作用域）、state（可变）、
    created_at/updated_at（生命周期）、payload_hash 自身。
    绝不能用 model_dump 整体 hash——会把 state/时间戳卷进指纹，
    导致 transition 后同内容 create 判为冲突。
    """
    return {
        "type": recommendation.type.value,
        "target": recommendation.target.model_dump(mode="json"),
        "facts_snapshot": recommendation.facts_snapshot,
        "rationale": recommendation.rationale,
        "missing_evidence": list(recommendation.missing_evidence),
        "alternatives": [a.value for a in recommendation.alternatives],
        "degraded": recommendation.degraded,
    }


def _audit_content_payload(
    recommendation_id: str, audit: AuditRecord
) -> dict[str, Any]:
    """审计内容指纹（排除 audit_id 自增、tenant_id 作用域、payload_hash 自身）。"""
    return {
        "recommendation_id": recommendation_id,
        "action": audit.action.value,
        "from_state": audit.from_state.value,
        "to_state": audit.to_state.value,
        "actor": audit.actor,
        "occurred_at": canonical_source_time(audit.at),
    }


class RecommendationPersistenceService:
    """v36 生命周期建议落库服务（thin，无业务逻辑）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- 写 ----

    def create(self, tenant_id: str, recommendation: Recommendation) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant_id)
        if recommendation.state is not RecommendationState.DRAFT:
            raise RecommendationError("recommendation_create_state_not_draft")
        validate_full_recommendation(recommendation)  # B3 + required_facts 写屏障语义
        payload_hash = payload_digest(_recommendation_content_payload(recommendation))
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT payload_hash FROM product_recommendations "
                "WHERE tenant_id=? AND recommendation_id=?",
                (tenant_id, recommendation.recommendation_id),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise RecommendationError("recommendation_conflict")
                write_status = "idempotent"
            else:
                conn.execute(
                    """
                    INSERT INTO product_recommendations(
                        recommendation_id, tenant_id, recommendation_type, store_id, item_id,
                        sku_id, facts_snapshot_json, rationale, missing_evidence_json,
                        alternatives_json, state, degraded, payload_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        recommendation.recommendation_id,
                        tenant_id,
                        recommendation.type.value,
                        recommendation.target.store_id,
                        recommendation.target.item_id,
                        recommendation.target.sku_id,
                        _json_dump(recommendation.facts_snapshot),
                        recommendation.rationale,
                        _json_dump(list(recommendation.missing_evidence)),
                        _json_dump([a.value for a in recommendation.alternatives]),
                        recommendation.state.value,
                        int(recommendation.degraded),
                        payload_hash,
                        canonical_source_time(recommendation.created_at),
                        canonical_source_time(recommendation.updated_at),
                    ),
                )
        result = self.get(tenant_id, recommendation.recommendation_id)
        result["write_status"] = write_status
        return result

    def record_transition(
        self,
        tenant_id: str,
        recommendation_id: str,
        *,
        action: TransitionAction,
        actor: str,
        at: datetime,
    ) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant_id)
        at_text = canonical_source_time(at)
        write_status = "applied"
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rec = conn.execute(
                "SELECT state FROM product_recommendations "
                "WHERE tenant_id=? AND recommendation_id=?",
                (tenant_id, recommendation_id),
            ).fetchone()
            if rec is None:
                raise RecommendationError("recommendation_not_found")
            # 幂等判重：同一逻辑事件（同 action+occurred_at+actor）重放 -> 复用
            existing = conn.execute(
                "SELECT * FROM product_recommendation_audit "
                "WHERE tenant_id=? AND recommendation_id=? AND action=? AND occurred_at=? AND actor=?",
                (tenant_id, recommendation_id, action.value, at_text, actor),
            ).fetchone()
            if existing is not None:
                write_status = "idempotent"
                audit_view = self.audit_view(dict(existing))
            else:
                new_state, audit = StateMachine(
                    RecommendationState(str(rec["state"]))
                ).apply(action, actor=actor, at=at, target=recommendation_id)
                audit_hash = payload_digest(
                    _audit_content_payload(recommendation_id, audit)
                )
                conn.execute(
                    "UPDATE product_recommendations SET state=?, updated_at=? "
                    "WHERE tenant_id=? AND recommendation_id=?",
                    (
                        new_state.value,
                        canonical_source_time(audit.at),
                        tenant_id,
                        recommendation_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO product_recommendation_audit(
                        tenant_id, recommendation_id, action, from_state, to_state,
                        actor, occurred_at, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        recommendation_id,
                        audit.action.value,
                        audit.from_state.value,
                        audit.to_state.value,
                        audit.actor,
                        canonical_source_time(audit.at),
                        audit_hash,
                    ),
                )
                audit_view = {
                    "audit_id": None,
                    "tenant_id": tenant_id,
                    "recommendation_id": recommendation_id,
                    "action": audit.action.value,
                    "from_state": audit.from_state.value,
                    "to_state": audit.to_state.value,
                    "actor": audit.actor,
                    "occurred_at": canonical_source_time(audit.at),
                    "payload_hash": audit_hash,
                }
        return {
            "recommendation": self.get(tenant_id, recommendation_id),
            "audit": audit_view,
            "write_status": write_status,
        }

    # ---- 读 ----

    def get(self, tenant_id: str, recommendation_id: str) -> dict[str, Any]:
        tenant_id = _tenant_id(tenant_id)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM product_recommendations "
                "WHERE tenant_id=? AND recommendation_id=?",
                (tenant_id, recommendation_id),
            ).fetchone()
        if row is None:
            raise RecommendationError("recommendation_not_found")
        return self.recommendation_view(dict(row))

    def list(
        self,
        tenant_id: str,
        *,
        store_id: str | None = None,
        state: RecommendationState | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        tenant_id = _tenant_id(tenant_id)
        limit = _bounded_limit(limit, default=100, maximum=1000)
        clauses = ["tenant_id=?"]
        params: list[Any] = [tenant_id]
        if store_id is not None:
            clauses.append("store_id=?")
            params.append(store_id)
        if state is not None:
            clauses.append("state=?")
            params.append(state.value)
        params.append(limit)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM product_recommendations WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, recommendation_id ASC LIMIT ?",
                params,
            ).fetchall()
        return [self.recommendation_view(dict(row)) for row in rows]

    def audit_trail(
        self,
        tenant_id: str,
        recommendation_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        tenant_id = _tenant_id(tenant_id)
        limit = _bounded_limit(limit, default=200, maximum=1000)
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM product_recommendation_audit "
                "WHERE tenant_id=? AND recommendation_id=? "
                "ORDER BY occurred_at ASC, audit_id ASC LIMIT ?",
                (tenant_id, recommendation_id, limit),
            ).fetchall()
        return [self.audit_view(dict(row)) for row in rows]

    # ---- 视图 / 模型 ----

    @staticmethod
    def recommendation_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "recommendation_id": str(row["recommendation_id"]),
            "tenant_id": str(row["tenant_id"]),
            "type": str(row["recommendation_type"]),
            "target": {
                "store_id": str(row["store_id"]),
                "item_id": str(row["item_id"]) if row["item_id"] else None,
                "sku_id": str(row["sku_id"]) if row["sku_id"] else None,
            },
            "facts_snapshot": _json_load(row["facts_snapshot_json"]),
            "rationale": str(row["rationale"]),
            "missing_evidence": _json_load(row["missing_evidence_json"]),
            "alternatives": _json_load(row["alternatives_json"]),
            "state": str(row["state"]),
            "degraded": bool(row["degraded"]),
            "payload_hash": str(row["payload_hash"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def audit_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "audit_id": int(row["audit_id"]),
            "tenant_id": str(row["tenant_id"]),
            "recommendation_id": str(row["recommendation_id"]),
            "action": str(row["action"]),
            "from_state": str(row["from_state"]),
            "to_state": str(row["to_state"]),
            "actor": str(row["actor"]),
            "occurred_at": str(row["occurred_at"]),
            "payload_hash": str(row["payload_hash"]),
        }

    @staticmethod
    def _from_row(row: dict[str, Any]) -> Recommendation:
        """view dict → Recommendation 模型（round-trip 测试用；工作台读侧用 view dict）。"""
        return Recommendation(
            recommendation_id=row["recommendation_id"],
            type=RecommendationType(row["type"]),
            target=TargetObject(
                store_id=row["target"]["store_id"],
                item_id=row["target"]["item_id"],
                sku_id=row["target"]["sku_id"],
            ),
            facts_snapshot=row["facts_snapshot"],
            rationale=row["rationale"],
            missing_evidence=row["missing_evidence"],
            alternatives=[RecommendationType(v) for v in row["alternatives"]],
            state=RecommendationState(row["state"]),
            degraded=row["degraded"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


__all__ = [
    "RecommendationError",
    "RecommendationPersistenceService",
]
