from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime
from typing import Any

from .database import Database
from .schemas import RetrievedDocument
from .text_utils import (
    blob_to_vector,
    checksum,
    cosine_similarity,
    hash_embedding,
    search_terms,
    search_text,
    vector_to_blob,
)


class KnowledgeBase:
    def __init__(self, db: Database):
        self.db = db

    def count_active(self, tenant_id: str | None = None) -> int:
        where = "status='active'"
        params: tuple[Any, ...] = ()
        if tenant_id is not None:
            where += " AND (tenant_id IS NULL OR tenant_id=?)"
            params = (tenant_id,)
        with self.db.connect() as conn:
            return int(
                conn.execute(f"SELECT COUNT(*) FROM knowledge WHERE {where}", params).fetchone()[0]
            )

    def seed_if_empty(self, records: list[dict[str, Any]]) -> int:
        with self.db.connect() as conn:
            global_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM knowledge WHERE status='active' AND tenant_id IS NULL"
                ).fetchone()[0]
            )
        if global_count > 0:
            return 0
        for record in records:
            self.add_document(
                **record,
                status="active",
                approved_by="builtin",
                tenant_id=None,
            )
        self.db.audit("knowledge.seeded", "system", None, {"count": len(records)})
        return len(records)

    def add_document(
        self,
        *,
        category: str,
        intent: str,
        question: str,
        answer: str,
        keywords: str,
        risk_level: str,
        source: str,
        version: int = 1,
        id: str | None = None,
        status: str = "active",
        approved_by: str | None = None,
        tenant_id: str | None = None,
        knowledge_key: str | None = None,
        layer: str = "industry",
        store_id: str | None = None,
        sku_id: str | None = None,
        review_status: str | None = None,
    ) -> str:
        document_id = id or f"kb-{uuid.uuid4().hex}"
        document_key = knowledge_key or document_id
        indexed_text = search_text(question, answer, keywords, category, intent)
        embedding = vector_to_blob(hash_embedding(f"{question} {keywords} {answer}"))
        now = datetime.now(UTC).isoformat()
        digest = checksum(
            question, answer, source, str(version), tenant_id or "global",
            layer, store_id or "", sku_id or "",
        )
        lifecycle = review_status or ("approved" if status == "active" else "draft")
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO knowledge(
                    id, category, intent, question, answer, keywords, search_text,
                    embedding, risk_level, source, version, status, effective_from,
                    effective_to, approved_by, checksum, created_at, tenant_id,
                    knowledge_key, layer, store_id, sku_id, review_status,
                    record_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    document_id, category, intent, question, answer, keywords,
                    indexed_text, embedding, risk_level, source, version, status,
                    now, approved_by, digest, now, tenant_id,
                    document_key, layer, store_id, sku_id, lifecycle, now,
                ),
            )
            conn.execute(
                "INSERT INTO knowledge_fts(doc_id, search_text) VALUES (?, ?)",
                (document_id, indexed_text),
            )
        return document_id

    def retrieve(
        self,
        query: str,
        *,
        top_k: int,
        min_score: float,
        intent: str | None = None,
        tenant_id: str | None = None,
        store_id: str | None = None,
        sku_id: str | None = None,
        rollout_unit: str | None = None,
    ) -> list[RetrievedDocument]:
        query_terms = set(search_terms(query))
        query_vector = hash_embedding(query)
        now = datetime.now(UTC).isoformat()
        with self.db.connect() as conn:
            tenant_clause = (
                "AND tenant_id IS NULL"
                if tenant_id is None
                else "AND (tenant_id IS NULL OR tenant_id=?)"
            )
            scope_clauses = []
            scope_params: list[Any] = []
            if store_id is None:
                scope_clauses.append("AND store_id IS NULL")
            else:
                scope_clauses.append("AND (store_id IS NULL OR store_id=?)")
                scope_params.append(store_id)
            if sku_id is None:
                scope_clauses.append("AND sku_id IS NULL")
            else:
                scope_clauses.append("AND (sku_id IS NULL OR sku_id=?)")
                scope_params.append(sku_id)
            params: tuple[Any, ...] = (
                (now, now, *scope_params)
                if tenant_id is None
                else (now, now, tenant_id, *scope_params)
            )
            rows = conn.execute(
                f"""
                SELECT id, knowledge_key, category, intent, question, answer, keywords,
                       search_text, embedding, source, version, layer, store_id,
                       sku_id, tenant_id
                FROM knowledge
                WHERE status='active' AND effective_from <= ?
                  AND (effective_to IS NULL OR effective_to > ?)
                  {tenant_clause}
                  {' '.join(scope_clauses)}
                """,
                params,
            ).fetchall()
        rows = self._apply_rollouts(
            [dict(row) for row in rows],
            tenant_id=tenant_id,
            rollout_unit=rollout_unit,
            scope_clauses=scope_clauses,
            scope_params=scope_params,
        )

        ranked: list[RetrievedDocument] = []
        for row in rows:
            score = self._score(query_terms, query_vector, row, intent)
            if score < min_score:
                continue
            ranked.append(
                RetrievedDocument(
                    id=row["id"], knowledge_key=row["knowledge_key"],
                    category=row["category"], intent=row["intent"],
                    question=row["question"], answer=row["answer"], source=row["source"],
                    version=row["version"], score=round(score, 4),
                    layer=row["layer"], store_id=row["store_id"], sku_id=row["sku_id"],
                    tenant_id=row["tenant_id"],
                )
            )
        ranked.sort(
            key=lambda item: (
                item["score"],
                # ① 多租户 tiebreak：本租户行优先于全局行（影子编辑生效的前提）。
                # 此前本租户影子行与全局行同分同 store NULL 同 version 时排序
                # 不稳定，seen_answers 去重还可能把影子答案丢掉。
                int(item["tenant_id"] == tenant_id),
                int(item["sku_id"] is not None),
                int(item["store_id"] is not None),
                item["version"],
            ),
            reverse=True,
        )

        # Avoid returning three paraphrases with the exact same answer.
        unique: list[RetrievedDocument] = []
        seen_answers: set[str] = set()
        for item in ranked:
            if item["answer"] in seen_answers:
                continue
            seen_answers.add(item["answer"])
            unique.append(item)
            if len(unique) >= top_k:
                break
        return unique

    def _apply_rollouts(
        self,
        rows: list[dict[str, Any]],
        *,
        tenant_id: str | None,
        rollout_unit: str | None,
        scope_clauses: list[str],
        scope_params: list[Any],
    ) -> list[dict[str, Any]]:
        """Serve gray-release candidates to in-bucket units, baselines to the rest.

        Without a rollout unit every caller stays on the approved baseline, so
        evaluation and evolution paths never observe half-released knowledge.
        """
        if tenant_id is None or rollout_unit is None:
            return rows
        from .rollouts import active_rollouts, rollout_choice

        chosen: dict[str, str] = {}
        for rollout in active_rollouts(self.db, tenant_id, "knowledge"):
            candidate_id = rollout_choice(rollout, rollout_unit)
            if candidate_id is not None:
                chosen[str(rollout["subject_key"])] = candidate_id
        if not chosen:
            return rows
        placeholders = ",".join("?" for _ in chosen)
        with self.db.connect() as conn:
            candidates = conn.execute(
                f"""
                SELECT id, knowledge_key, category, intent, question, answer, keywords,
                       search_text, embedding, source, version, layer, store_id,
                       sku_id, tenant_id
                FROM knowledge
                WHERE status='candidate' AND tenant_id=? AND id IN ({placeholders})
                  {' '.join(scope_clauses)}
                """,
                (tenant_id, *chosen.values(), *scope_params),
            ).fetchall()
        replaced = [
            row for row in rows if str(row["knowledge_key"]) not in chosen
        ]
        replaced.extend(dict(row) for row in candidates)
        return replaced

    def candidate_score(
        self,
        query: str,
        *,
        intent: str,
        query_intent: str | None = None,
        question: str,
        answer: str,
        keywords: str = "",
        category: str = "进化话术",
    ) -> float:
        record = {
            "intent": intent,
            "search_text": search_text(question, answer, keywords, category, intent),
            "embedding": vector_to_blob(hash_embedding(f"{question} {keywords} {answer}")),
        }
        return round(
            self._score(
                set(search_terms(query)),
                hash_embedding(query),
                record,
                query_intent if query_intent is not None else intent,
            ),
            4,
        )

    @staticmethod
    def _score(
        query_terms: set[str],
        query_vector: list[float],
        document: Any,
        intent: str | None,
    ) -> float:
        document_terms = set(document["search_text"].split())
        intersection = len(query_terms & document_terms)
        lexical = intersection / max(1.0, math.sqrt(len(query_terms) * len(document_terms)))
        semantic = max(
            0.0,
            cosine_similarity(query_vector, blob_to_vector(document["embedding"])),
        )
        intent_bonus = 0.12 if intent and document["intent"] == intent else 0.0
        return 0.55 * semantic + 0.45 * lexical + intent_bonus

    def next_version(self, intent: str, tenant_id: str | None = None) -> int:
        # 多租户低项：版本命名空间按租户隔离——租户 evolution 的版本号
        # 不受全局同 intent 行影响（此前 NULL 分支混入全局行导致跳号/撞号）
        tenant_clause = (
            "tenant_id IS NULL" if tenant_id is None else "tenant_id=?"
        )
        params: tuple[Any, ...] = (intent,) if tenant_id is None else (intent, tenant_id)
        with self.db.connect() as conn:
            row = conn.execute(
                f"SELECT COALESCE(MAX(version), 0) + 1 FROM knowledge "
                f"WHERE intent=? AND {tenant_clause}",
                params,
            ).fetchone()
        return int(row[0])

    def get_document(
        self, document_id: str, tenant_id: str | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM knowledge WHERE id = ?"
        params: tuple[Any, ...] = (document_id,)
        if tenant_id is not None:
            query += " AND tenant_id = ?"
            params = (document_id, tenant_id)
        with self.db.connect() as conn:
            row = conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def retire_document(
        self, document_id: str, actor: str, tenant_id: str | None = None
    ) -> bool:
        tenant_clause = "tenant_id IS NULL" if tenant_id is None else "tenant_id=?"
        params: tuple[Any, ...] = (
            (datetime.now(UTC).isoformat(), document_id)
            if tenant_id is None
            else (datetime.now(UTC).isoformat(), document_id, tenant_id)
        )
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                # 多租户低项：retire 递增 record_version（乐观锁可见性，
                # 对齐 knowledge_management 口径）
                f"UPDATE knowledge SET status='retired', effective_to=?, "
                f"record_version=record_version+1 "
                f"WHERE id=? AND status='active' AND {tenant_clause}",
                params,
            )
        changed = cursor.rowcount == 1
        if changed:
            self.db.audit("knowledge.retired", actor, document_id, {}, tenant_id)
        return changed
