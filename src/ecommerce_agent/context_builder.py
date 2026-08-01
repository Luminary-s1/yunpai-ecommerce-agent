from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from . import product_advisor
from .database import Database, utc_now
from .text_utils import redact_sensitive
from .tokens import count_messages, truncate_history


ContextStage = Literal["decision", "generation"]
ContextReadiness = Literal["ready", "clarification_required", "handoff_required"]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "[DEPTH_LIMIT]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return redact_sensitive(value[:2000])[0]
    if isinstance(value, dict):
        return {
            str(key)[:128]: _safe_value(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:100]]
    return redact_sensitive(str(value)[:500])[0]


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    id: str
    tenant_id: str
    session_id: str
    trace_id: str
    stage: ContextStage
    sequence: int
    parent_snapshot_id: str | None
    readiness: ContextReadiness
    bundle: dict[str, Any]
    evidence: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    missing: list[str]
    checksum: str
    created_at: str

    @property
    def evidence_ids(self) -> list[str]:
        return [str(item["evidence_id"]) for item in self.evidence]

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": self.stage,
            "sequence": self.sequence,
            "parent_snapshot_id": self.parent_snapshot_id,
            "readiness": self.readiness,
            "evidence_count": len(self.evidence),
            "conflict_count": len(self.conflicts),
            "missing": self.missing,
            "checksum": self.checksum,
            "created_at": self.created_at,
        }


class ContextBuilder:
    """Build and persist replayable, tenant-scoped context snapshots."""

    CONTEXT_VERSION = "context.v1"
    RESTRICTED_ORDER_FIELDS = {
        "order_id",
        "order_status",
        "logistics_status",
        "carrier",
        "tracking_last_event",
    }

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _evidence(
        evidence_type: str,
        source_id: str,
        *,
        authority: str,
        freshness: str,
        source_version: str | int | None = None,
        summary: dict[str, Any] | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "type": evidence_type,
            "source_id": source_id,
            "source_version": source_version,
            "authority": authority,
            "freshness": freshness,
            "observed_at": observed_at,
            "summary": _safe_value(summary or {}),
        }
        checksum = _digest(body)
        return {
            "evidence_id": f"ev-{checksum[:24]}",
            **body,
            "checksum": checksum,
        }

    @staticmethod
    def _conflicts(context: dict[str, Any]) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        aliases = (("store_id", "shop_id"), ("sku_id", "sku"))
        for canonical, alias in aliases:
            if context.get(canonical) and context.get(alias) and context[canonical] != context[alias]:
                conflicts.append(
                    {
                        "code": f"{canonical}_identity_conflict",
                        "severity": "high",
                        "fields": [canonical, alias],
                    }
                )
        restricted = sorted(
            key
            for key in ContextBuilder.RESTRICTED_ORDER_FIELDS
            if context.get(key) and context.get("authorized") is not True
        )
        if restricted:
            conflicts.append(
                {
                    "code": "unauthorized_order_context",
                    "severity": "critical",
                    "fields": restricted,
                }
            )
        return conflicts

    def build(
        self,
        *,
        tenant_id: str,
        session_id: str,
        trace_id: str,
        stage: ContextStage,
        sequence: int,
        question: str,
        trusted_context: dict[str, Any],
        documents: list[dict[str, Any]],
        sops: list[dict[str, Any]],
        tool_catalog: list[dict[str, Any]],
        history: list[dict[str, Any]],
        history_budget_tokens: int | None = None,
        tool_result: dict[str, Any] | None = None,
        parent_snapshot_id: str | None = None,
    ) -> ContextSnapshot:
        safe_context = _safe_value(trusted_context)
        safe_documents = _safe_value(documents)
        safe_sops = _safe_value(sops)
        safe_tools = _safe_value(tool_catalog)
        history_budget = (
            count_messages(history)
            if history_budget_tokens is None
            else history_budget_tokens
        )
        recent_history, recent_history_meta = truncate_history(
            history,
            budget_tokens=history_budget,
        )
        safe_history = _safe_value(recent_history)
        safe_tool_result = _safe_value(tool_result or {})
        conflicts = self._conflicts(safe_context)
        if safe_context.get("authorized") is not True:
            safe_context = {
                key: value
                for key, value in safe_context.items()
                if key not in self.RESTRICTED_ORDER_FIELDS
            }
        missing: list[str] = []
        readiness: ContextReadiness = (
            "handoff_required"
            if any(item["severity"] in {"high", "critical"} for item in conflicts)
            else "ready"
        )

        evidence: list[dict[str, Any]] = [
            self._evidence(
                "trusted_session",
                f"session:{_digest({'tenant_id': tenant_id, 'session_id': session_id})[:24]}",
                authority="authenticated_session",
                freshness="current",
                source_version=self.CONTEXT_VERSION,
                summary={"tenant_scoped": True, "history_messages": len(safe_history)},
            ),
            self._evidence(
                "history_window",
                f"history:{_digest({'session_id': session_id, 'history': safe_history})[:24]}",
                authority="context_budget",
                freshness="current",
                source_version=self.CONTEXT_VERSION,
                summary=recent_history_meta,
            ),
        ]
        if safe_context:
            evidence.append(
                self._evidence(
                    "business_context",
                    f"context:{_digest(safe_context)[:24]}",
                    authority=(
                        "authorized_platform_context"
                        if safe_context.get("authorized") is True
                        else "sanitized_request_context"
                    ),
                    freshness="current",
                    source_version=self.CONTEXT_VERSION,
                    summary=safe_context,
                )
            )
        for item in safe_documents:
            evidence.append(
                self._evidence(
                    "knowledge",
                    str(item.get("id", "unknown")),
                    authority="approved_knowledge",
                    freshness="active",
                    source_version=item.get("version"),
                    summary={
                        "category": item.get("category"),
                        "layer": item.get("layer"),
                        "source": item.get("source"),
                        "score": item.get("score"),
                        "store_id": item.get("store_id"),
                        "sku_id": item.get("sku_id"),
                    },
                )
            )
        for item in safe_sops:
            evidence.append(
                self._evidence(
                    "sop",
                    str(item.get("version_id") or item.get("id") or "unknown"),
                    authority="active_sop",
                    freshness="active",
                    source_version=item.get("version"),
                    summary={
                        "sop_key": item.get("sop_key"),
                        "intent": item.get("intent"),
                        "risk_level": item.get("risk_level"),
                        "required_context": item.get("required_context", []),
                    },
                )
            )
        advisor = product_advisor.advise(
            self.db,
            tenant_id=tenant_id,
            store_id=safe_context.get("store_id") or safe_context.get("shop_id"),
            question=question,
            history=safe_history,
        )
        safe_advisor = _safe_value(advisor)
        for candidate in advisor["candidates"]:
            evidence.append(
                self._evidence(
                    "catalog_item",
                    str(candidate["evidence_id"]),
                    authority="versioned_catalog_fact",
                    freshness="active",
                    source_version=candidate["version"],
                    observed_at=str(candidate["source_updated_at"]),
                    summary={
                        "sku_id": candidate["sku_id"],
                        "title": candidate["title"],
                        "sale_price": candidate["sale_price"],
                        "currency": candidate["currency"],
                        "score": candidate["score"],
                    },
                )
            )
        evidence.append(
            self._evidence(
                "tool_catalog",
                f"tools:{_digest(safe_tools)[:24]}",
                authority="registered_capabilities",
                freshness="current",
                source_version=self.CONTEXT_VERSION,
                summary={
                    "tools": [
                        {"name": item.get("name"), "kind": item.get("kind")}
                        for item in safe_tools
                    ]
                },
            )
        )
        if safe_tool_result:
            verified = safe_tool_result.get("postcondition_met") is True
            evidence.append(
                self._evidence(
                    "tool_result",
                    f"tool:{safe_tool_result.get('tool_name', 'unknown')}:{sequence}",
                    authority="verified_tool" if verified else "unverified_tool_observation",
                    freshness="current",
                    source_version=self.CONTEXT_VERSION,
                    summary=safe_tool_result,
                )
            )
            if not verified:
                conflicts.append(
                    {
                        "code": "tool_result_not_verified",
                        "severity": "medium" if stage == "decision" else "high",
                        "fields": ["tool_result.postcondition_met"],
                    }
                )
                if stage == "generation":
                    readiness = "handoff_required"

        bundle = {
            "context_version": self.CONTEXT_VERSION,
            "trusted_session_state": {
                "tenant_scoped": True,
                "business_context_authorized": safe_context.get("authorized") is True,
                "platform": safe_context.get("platform"),
                "store_id": safe_context.get("store_id") or safe_context.get("shop_id"),
            },
            "current_subject": {
                key: safe_context[key]
                for key in (
                    "product_name", "sku_id", "sku", "order_id", "order_status",
                    "logistics_status", "carrier", "tracking_last_event", "shop_policy",
                )
                if key in safe_context
            },
            "sop_evidence": safe_sops,
            "knowledge_evidence": safe_documents,
            "product_advisor": safe_advisor,
            "available_tools": safe_tools,
            "output_constraints": {
                "language": "zh-CN",
                "channel": safe_context.get("platform") or "api",
                "verified_business_result_required": True,
                "conflict_policy": "handoff",
            },
            "recent_history": safe_history,
            "recent_history_meta": recent_history_meta,
            "latest_tool_result": safe_tool_result,
        }
        payload = {
            "context_version": self.CONTEXT_VERSION,
            "stage": stage,
            "sequence": sequence,
            "parent_snapshot_id": parent_snapshot_id,
            "request_hash": _digest({"question": question}),
            "bundle": bundle,
            "evidence": evidence,
            "conflicts": conflicts,
            "missing": missing,
            "readiness": readiness,
        }
        checksum = _digest(payload)
        snapshot_id = f"ctx-{uuid.uuid4().hex}"
        created_at = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM context_snapshots
                WHERE tenant_id=? AND trace_id=? AND stage=? AND sequence=?
                """,
                (tenant_id, trace_id, stage, sequence),
            ).fetchone()
            if existing is not None:
                snapshot = self._from_row(dict(existing))
                if snapshot.checksum != checksum:
                    raise RuntimeError("context snapshot replay mismatch")
                return snapshot
            try:
                conn.execute(
                    """
                    INSERT INTO context_snapshots(
                        id, tenant_id, session_id, trace_id, stage, sequence,
                        parent_snapshot_id, context_version, request_hash, bundle_json,
                        evidence_json, conflicts_json, missing_json, readiness,
                        checksum, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id, tenant_id, session_id, trace_id, stage, sequence,
                        parent_snapshot_id, self.CONTEXT_VERSION, payload["request_hash"],
                        _canonical_json(bundle), _canonical_json(evidence),
                        _canonical_json(conflicts), _canonical_json(missing), readiness,
                        checksum, created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("context snapshot persistence conflict") from exc
        return ContextSnapshot(
            id=snapshot_id,
            tenant_id=tenant_id,
            session_id=session_id,
            trace_id=trace_id,
            stage=stage,
            sequence=sequence,
            parent_snapshot_id=parent_snapshot_id,
            readiness=readiness,
            bundle=bundle,
            evidence=evidence,
            conflicts=conflicts,
            missing=missing,
            checksum=checksum,
            created_at=created_at,
        )

    def get(self, tenant_id: str, snapshot_id: str) -> ContextSnapshot | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM context_snapshots WHERE id=? AND tenant_id=?",
                (snapshot_id, tenant_id),
            ).fetchone()
        if row is None:
            return None
        snapshot = self._from_row(dict(row))
        payload = {
            "context_version": self.CONTEXT_VERSION,
            "stage": snapshot.stage,
            "sequence": snapshot.sequence,
            "parent_snapshot_id": snapshot.parent_snapshot_id,
            "request_hash": row["request_hash"],
            "bundle": snapshot.bundle,
            "evidence": snapshot.evidence,
            "conflicts": snapshot.conflicts,
            "missing": snapshot.missing,
            "readiness": snapshot.readiness,
        }
        if _digest(payload) != snapshot.checksum:
            raise RuntimeError("context snapshot checksum mismatch")
        return snapshot

    @staticmethod
    def _from_row(row: dict[str, Any]) -> ContextSnapshot:
        return ContextSnapshot(
            id=row["id"],
            tenant_id=row["tenant_id"],
            session_id=row["session_id"],
            trace_id=row["trace_id"],
            stage=row["stage"],
            sequence=int(row["sequence"]),
            parent_snapshot_id=row["parent_snapshot_id"],
            readiness=row["readiness"],
            bundle=json.loads(row["bundle_json"]),
            evidence=json.loads(row["evidence_json"]),
            conflicts=json.loads(row["conflicts_json"]),
            missing=json.loads(row["missing_json"]),
            checksum=row["checksum"],
            created_at=row["created_at"],
        )
