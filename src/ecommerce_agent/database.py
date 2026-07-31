from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


SESSION_SOURCE_TYPES = {"api", "channel", "simulation", "evaluation"}
SESSION_SCOPES = {"operational", "simulation", "evaluation", "all"}


def session_scope_condition(scope: str, alias: str = "s") -> str:
    if scope not in SESSION_SCOPES:
        raise ValueError("invalid session scope")
    if scope == "operational":
        return f"{alias}.source_type NOT IN ('simulation','evaluation')"
    if scope == "simulation":
        return f"{alias}.source_type='simulation'"
    if scope == "evaluation":
        return f"{alias}.source_type='evaluation'"
    return "1=1"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class SessionScopeError(ValueError):
    pass


class Database:
    SCHEMA_VERSION = 25

    def __init__(self, path: Path):
        self.path = path
        self._write_lock = threading.RLock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 20000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            legacy_v1 = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge'"
            ).fetchone()
            if 1 not in applied:
                if legacy_v1 is None:
                    self._apply_v1(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (1, ?)", (utc_now(),))
            if 2 not in applied:
                self._apply_v2(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (2, ?)", (utc_now(),))
            if 3 not in applied:
                self._apply_v3(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (3, ?)", (utc_now(),))
            if 4 not in applied:
                self._apply_v4(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (4, ?)", (utc_now(),))
            if 5 not in applied:
                self._apply_v5(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (5, ?)", (utc_now(),))
            if 6 not in applied:
                self._apply_v6(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (6, ?)", (utc_now(),))
            if 7 not in applied:
                self._apply_v7(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (7, ?)", (utc_now(),))
            if 8 not in applied:
                self._apply_v8(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (8, ?)", (utc_now(),))
            if 9 not in applied:
                self._apply_v9(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (9, ?)", (utc_now(),))
            if 10 not in applied:
                self._apply_v10(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (10, ?)", (utc_now(),))
            if 11 not in applied:
                self._apply_v11(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (11, ?)", (utc_now(),))
            if 12 not in applied:
                self._apply_v12(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (12, ?)", (utc_now(),))
            if 13 not in applied:
                self._apply_v13(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (13, ?)", (utc_now(),))
            if 14 not in applied:
                self._apply_v14(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (14, ?)", (utc_now(),))
            if 15 not in applied:
                self._apply_v15(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (15, ?)", (utc_now(),))
            if 16 not in applied:
                self._apply_v16(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (16, ?)", (utc_now(),))
            if 17 not in applied:
                self._apply_v17(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (17, ?)", (utc_now(),))
            if 18 not in applied:
                self._apply_v18(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (18, ?)", (utc_now(),))
            if 19 not in applied:
                self._apply_v19(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (19, ?)", (utc_now(),))
            if 20 not in applied:
                self._apply_v20(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (20, ?)", (utc_now(),))
            if 21 not in applied:
                self._apply_v21(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (21, ?)", (utc_now(),))
            if 22 not in applied:
                self._apply_v22(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (22, ?)", (utc_now(),))
            if 23 not in applied:
                self._apply_v23(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (23, ?)", (utc_now(),))
            if 24 not in applied:
                self._apply_v24(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (24, ?)", (utc_now(),))
            if 25 not in applied:
                self._apply_v25(conn)
                conn.execute("INSERT INTO schema_migrations VALUES (25, ?)", (utc_now(),))
            conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            self._validate_schema(conn)

    @staticmethod
    def _apply_v1(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                intent TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '',
                search_text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                risk_level TEXT NOT NULL DEFAULT 'low',
                source TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL CHECK(status IN ('active','candidate','retired')),
                effective_from TEXT NOT NULL,
                effective_to TEXT,
                approved_by TEXT,
                checksum TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_knowledge_status_intent
                ON knowledge(status, intent);

            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                doc_id UNINDEXED,
                search_text,
                tokenize='unicode61'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                content TEXT NOT NULL,
                intent TEXT,
                risk_level TEXT,
                route_reason TEXT,
                sources_json TEXT NOT NULL DEFAULT '[]',
                model_fallback INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session_time
                ON messages(session_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL REFERENCES messages(id),
                rating INTEGER NOT NULL CHECK(rating IN (-1, 1)),
                corrected_answer TEXT,
                note TEXT,
                submitted_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS evolution_candidates (
                id TEXT PRIMARY KEY,
                feedback_id TEXT NOT NULL REFERENCES feedback(id),
                question TEXT NOT NULL,
                proposed_answer TEXT NOT NULL,
                intent TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','evaluated','approved','rejected')),
                gate_passed INTEGER,
                gate_report_json TEXT,
                resulting_knowledge_id TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decided_by TEXT,
                decision_note TEXT
            );

            CREATE TABLE IF NOT EXISTS evolution_runs (
                id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES evolution_candidates(id),
                passed INTEGER NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                subject_id TEXT,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    @classmethod
    def _apply_v2(cls, conn: sqlite3.Connection) -> None:
        cls._ensure_column(conn, "messages", "tenant_id", "TEXT")
        cls._ensure_column(conn, "messages", "client_id", "TEXT")
        cls._ensure_column(conn, "messages", "redacted", "INTEGER NOT NULL DEFAULT 0")
        cls._ensure_column(conn, "audit_log", "tenant_id", "TEXT")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_clients (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                key_salt BLOB NOT NULL,
                key_hash BLOB NOT NULL,
                key_iterations INTEGER NOT NULL,
                can_supply_order_context INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL CHECK(status IN ('active','disabled')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_api_clients_tenant_status
                ON api_clients(tenant_id, status);

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                external_session_id TEXT NOT NULL,
                subject_hash TEXT NOT NULL,
                client_id TEXT NOT NULL REFERENCES api_clients(id),
                status TEXT NOT NULL CHECK(status IN ('active','closed')),
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(tenant_id, external_session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_tenant_subject
                ON sessions(tenant_id, subject_hash, last_seen_at DESC);

            CREATE TABLE IF NOT EXISTS handoff_tasks (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                message_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN (
                    'proposed','accepted','working','input_required','review',
                    'completed','failed','canceled'
                )),
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                assigned_to TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_handoffs_tenant_status_time
                ON handoff_tasks(tenant_id, status, created_at DESC);

            CREATE TABLE IF NOT EXISTS request_metrics (
                trace_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                intent TEXT NOT NULL,
                route_reason TEXT NOT NULL,
                success INTEGER NOT NULL,
                model_fallback INTEGER NOT NULL,
                requires_human INTEGER NOT NULL,
                duration_ms REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_metrics_tenant_time
                ON request_metrics(tenant_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS retention_runs (
                id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )

    @classmethod
    def _apply_v3(cls, conn: sqlite3.Connection) -> None:
        cls._ensure_column(conn, "api_clients", "role", "TEXT NOT NULL DEFAULT 'client'")
        cls._ensure_column(conn, "feedback", "tenant_id", "TEXT")
        cls._ensure_column(conn, "evolution_candidates", "tenant_id", "TEXT")
        cls._ensure_column(conn, "evolution_runs", "tenant_id", "TEXT")
        conn.executescript(
            """
            DROP INDEX IF EXISTS idx_handoffs_tenant_status_time;
            ALTER TABLE handoff_tasks RENAME TO handoff_tasks_v2;

            CREATE TABLE handoff_tasks (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                message_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN (
                    'proposed','accepted','rejected','working','input_required','review',
                    'completed','failed','canceled'
                )),
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                acceptance_criteria TEXT NOT NULL,
                assigned_to TEXT,
                deadline_at TEXT,
                max_retries INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            INSERT INTO handoff_tasks(
                id, tenant_id, session_id, message_id, status, reason, payload_json,
                acceptance_criteria, assigned_to, deadline_at, max_retries, retry_count,
                version, created_at, updated_at, completed_at
            )
            SELECT
                id, tenant_id, session_id, message_id, status, reason, payload_json,
                '人工核对问题、记录处理结果并完成复核', assigned_to, NULL, 0, 0,
                version, created_at, updated_at, completed_at
            FROM handoff_tasks_v2;

            DROP TABLE handoff_tasks_v2;
            CREATE INDEX idx_handoffs_tenant_status_time
                ON handoff_tasks(tenant_id, status, created_at DESC);
            """
        )

    @classmethod
    def _apply_v4(cls, conn: sqlite3.Connection) -> None:
        cls._ensure_column(conn, "knowledge", "tenant_id", "TEXT")
        cls._ensure_column(conn, "feedback", "evidence_source", "TEXT")
        cls._ensure_column(conn, "evolution_candidates", "evidence_source", "TEXT")
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_tenant_status_intent
                ON knowledge(tenant_id, status, intent, effective_from);
            CREATE INDEX IF NOT EXISTS idx_evolution_candidates_tenant_status
                ON evolution_candidates(tenant_id, status, created_at DESC);
            """
        )

    @staticmethod
    def _apply_v5(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_connections (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                shop_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','authorized','disabled','error')),
                account_id TEXT,
                account_nick TEXT,
                credential_ciphertext TEXT,
                token_expires_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, platform, shop_id)
            );
            CREATE INDEX IF NOT EXISTS idx_platform_connections_lookup
                ON platform_connections(platform, status, shop_id);

            CREATE TABLE IF NOT EXISTS platform_oauth_states (
                state_hash TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                shop_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_platform_oauth_states_expiry
                ON platform_oauth_states(platform, expires_at);

            CREATE TABLE IF NOT EXISTS channel_conversations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                shop_id TEXT NOT NULL,
                external_conversation_id TEXT NOT NULL,
                buyer_hash TEXT NOT NULL,
                buyer_nick_masked TEXT,
                owner_mode TEXT NOT NULL CHECK(owner_mode IN ('bot','human','paused')),
                assigned_to TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                last_event_id TEXT,
                last_message_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, platform, shop_id, external_conversation_id)
            );
            CREATE INDEX IF NOT EXISTS idx_channel_conversations_owner
                ON channel_conversations(tenant_id, platform, owner_mode, updated_at DESC);

            CREATE TABLE IF NOT EXISTS channel_events (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                shop_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL REFERENCES channel_conversations(id),
                external_event_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
                message_type TEXT NOT NULL,
                content_redacted TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                routing_ciphertext TEXT,
                request_id TEXT,
                action_mode TEXT,
                status TEXT NOT NULL CHECK(status IN ('received','queued','sent','failed','ignored')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, platform, shop_id, external_event_id, direction)
            );
            CREATE INDEX IF NOT EXISTS idx_channel_events_conversation
                ON channel_events(conversation_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS channel_outbox (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL REFERENCES channel_conversations(id),
                event_id TEXT REFERENCES channel_events(id),
                idempotency_key TEXT NOT NULL,
                content_redacted TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','sending','sent','failed')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                platform_result_json TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_channel_outbox_status
                ON channel_outbox(tenant_id, status, created_at);
            """
        )

    @staticmethod
    def _apply_v6(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inventory_balances (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                warehouse_id TEXT NOT NULL,
                sku_id TEXT NOT NULL,
                on_hand TEXT NOT NULL,
                reserved TEXT NOT NULL,
                inbound TEXT NOT NULL,
                average_daily_sales TEXT NOT NULL,
                source_id TEXT,
                source_updated_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, connector_id, store_id, warehouse_id, sku_id)
            );
            CREATE INDEX IF NOT EXISTS idx_inventory_balances_lookup
                ON inventory_balances(tenant_id, store_id, sku_id, warehouse_id);

            CREATE TABLE IF NOT EXISTS competitor_observations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                subject_sku TEXT NOT NULL,
                competitor_name TEXT NOT NULL,
                competitor_sku TEXT NOT NULL,
                subject_price TEXT NOT NULL,
                competitor_price TEXT NOT NULL,
                currency TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN (
                    'authorized_api','licensed_provider','manual','file_import','virtual'
                )),
                source_ref TEXT NOT NULL,
                is_estimate INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                source_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(
                    tenant_id, connector_id, store_id, subject_sku,
                    competitor_sku, observed_at
                )
            );
            CREATE INDEX IF NOT EXISTS idx_competitor_observations_lookup
                ON competitor_observations(tenant_id, subject_sku, observed_at DESC);

            CREATE TABLE IF NOT EXISTS connector_sync_runs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                resource TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
                cursor_before TEXT,
                cursor_after TEXT,
                items_received INTEGER NOT NULL DEFAULT 0,
                items_applied INTEGER NOT NULL DEFAULT 0,
                data_as_of TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_connector_sync_runs_status
                ON connector_sync_runs(tenant_id, connector_id, status, created_at DESC);
            """
        )

    @staticmethod
    def _apply_v7(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalog_items (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                sku_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('draft','active','inactive','deleted')),
                sale_price TEXT NOT NULL,
                currency TEXT NOT NULL,
                attributes_json TEXT NOT NULL,
                source_id TEXT,
                source_updated_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, connector_id, store_id, sku_id)
            );
            CREATE INDEX IF NOT EXISTS idx_catalog_items_lookup
                ON catalog_items(tenant_id, store_id, sku_id, status);

            CREATE TABLE IF NOT EXISTS commerce_orders (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                external_order_id TEXT NOT NULL,
                order_status TEXT NOT NULL,
                payment_status TEXT NOT NULL,
                currency TEXT NOT NULL,
                total_amount TEXT NOT NULL,
                placed_at TEXT NOT NULL,
                buyer_ref_hash TEXT,
                source_id TEXT,
                source_updated_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, connector_id, store_id, external_order_id)
            );
            CREATE INDEX IF NOT EXISTS idx_commerce_orders_lookup
                ON commerce_orders(tenant_id, store_id, external_order_id, order_status);
            CREATE INDEX IF NOT EXISTS idx_commerce_orders_time
                ON commerce_orders(tenant_id, placed_at DESC);

            CREATE TABLE IF NOT EXISTS commerce_order_lines (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES commerce_orders(id) ON DELETE CASCADE,
                external_line_id TEXT NOT NULL,
                sku_id TEXT NOT NULL,
                title TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                unit_price TEXT NOT NULL,
                UNIQUE(order_id, external_line_id)
            );
            CREATE INDEX IF NOT EXISTS idx_commerce_order_lines_sku
                ON commerce_order_lines(sku_id, order_id);

            CREATE TABLE IF NOT EXISTS commerce_order_logistics (
                order_id TEXT PRIMARY KEY REFERENCES commerce_orders(id) ON DELETE CASCADE,
                carrier TEXT NOT NULL,
                tracking_no_masked TEXT NOT NULL,
                status TEXT NOT NULL,
                last_event TEXT NOT NULL,
                last_event_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS commerce_after_sale_cases (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES commerce_orders(id) ON DELETE CASCADE,
                external_case_id TEXT NOT NULL,
                case_type TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_amount TEXT NOT NULL,
                approved_amount TEXT NOT NULL,
                reason_code TEXT,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(order_id, external_case_id)
            );
            CREATE INDEX IF NOT EXISTS idx_after_sale_cases_status
                ON commerce_after_sale_cases(status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS commerce_order_events (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES commerce_orders(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                source_updated_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(order_id, version),
                UNIQUE(order_id, source_updated_at)
            );
            CREATE INDEX IF NOT EXISTS idx_commerce_order_events_order
                ON commerce_order_events(order_id, version DESC);
            """
        )
    @staticmethod
    def _apply_v8(conn: sqlite3.Connection) -> None:
        Database._ensure_column(
            conn,
            "competitor_observations",
            "payload_hash",
            "TEXT NOT NULL DEFAULT ''",
        )

    @classmethod
    def _apply_v9(cls, conn: sqlite3.Connection) -> None:
        cls._ensure_column(conn, "knowledge", "knowledge_key", "TEXT")
        cls._ensure_column(conn, "knowledge", "layer", "TEXT NOT NULL DEFAULT 'industry'")
        cls._ensure_column(conn, "knowledge", "store_id", "TEXT")
        cls._ensure_column(conn, "knowledge", "sku_id", "TEXT")
        cls._ensure_column(conn, "knowledge", "review_status", "TEXT NOT NULL DEFAULT 'approved'")
        cls._ensure_column(conn, "knowledge", "record_version", "INTEGER NOT NULL DEFAULT 1")
        cls._ensure_column(conn, "knowledge", "updated_at", "TEXT")
        conn.execute("UPDATE knowledge SET knowledge_key=id WHERE knowledge_key IS NULL")
        conn.execute("UPDATE knowledge SET updated_at=created_at WHERE updated_at IS NULL")
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_knowledge_scope
                ON knowledge(tenant_id, layer, store_id, sku_id, status, intent);
            CREATE INDEX IF NOT EXISTS idx_knowledge_key_version
                ON knowledge(tenant_id, knowledge_key, version DESC);

            CREATE TABLE IF NOT EXISTS sop_definitions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                sop_key TEXT NOT NULL,
                name TEXT NOT NULL,
                intent TEXT NOT NULL,
                risk_level TEXT NOT NULL CHECK(risk_level IN ('low','medium','high','critical')),
                current_active_version INTEGER,
                record_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, sop_key)
            );
            CREATE INDEX IF NOT EXISTS idx_sop_definitions_intent
                ON sop_definitions(tenant_id, intent, updated_at DESC);

            CREATE TABLE IF NOT EXISTS sop_versions (
                id TEXT PRIMARY KEY,
                definition_id TEXT NOT NULL REFERENCES sop_definitions(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                dsl_json TEXT NOT NULL,
                checksum TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('draft','evaluated','approved','active','retired')),
                evaluation_json TEXT,
                created_by TEXT NOT NULL,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                evaluated_at TEXT,
                approved_at TEXT,
                activated_at TEXT,
                retired_at TEXT,
                UNIQUE(definition_id, version)
            );
            CREATE INDEX IF NOT EXISTS idx_sop_versions_status
                ON sop_versions(definition_id, status, version DESC);

            CREATE TABLE IF NOT EXISTS sop_runs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                definition_id TEXT NOT NULL REFERENCES sop_definitions(id),
                sop_version_id TEXT NOT NULL REFERENCES sop_versions(id),
                status TEXT NOT NULL CHECK(status IN ('active','completed','handoff','failed')),
                outcome_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(session_id, definition_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sop_runs_session
                ON sop_runs(tenant_id, session_id, status);

            CREATE TABLE IF NOT EXISTS qa_results (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                conversation_type TEXT NOT NULL CHECK(conversation_type IN ('agent','channel')),
                conversation_id TEXT NOT NULL,
                assistant_message_id TEXT,
                ruleset_version TEXT NOT NULL,
                issues_json TEXT NOT NULL,
                score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
                review_status TEXT NOT NULL CHECK(review_status IN ('pending','confirmed','dismissed')),
                reviewer TEXT,
                correction TEXT,
                record_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_qa_results_tenant_status
                ON qa_results(tenant_id, review_status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_qa_results_conversation
                ON qa_results(tenant_id, conversation_type, conversation_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS channel_reply_drafts (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL REFERENCES channel_conversations(id) ON DELETE CASCADE,
                source_event_id TEXT REFERENCES channel_events(id),
                ai_suggestion_redacted TEXT NOT NULL,
                final_text_redacted TEXT NOT NULL,
                diff_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                sop_reference_json TEXT,
                confidence REAL,
                risk_level TEXT NOT NULL DEFAULT 'low',
                status TEXT NOT NULL CHECK(status IN ('draft','sending','sent','failed')),
                idempotency_key TEXT NOT NULL,
                outbox_id TEXT REFERENCES channel_outbox(id),
                last_error TEXT,
                record_version INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL,
                sent_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                UNIQUE(tenant_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_channel_reply_drafts_conversation
                ON channel_reply_drafts(tenant_id, conversation_id, created_at DESC);
            """
        )
        cls._ensure_column(
            conn,
            "channel_outbox",
            "delivery_state",
            "TEXT NOT NULL DEFAULT 'pending'",
        )

    @classmethod
    def _apply_v10(cls, conn: sqlite3.Connection) -> None:
        columns = (
            ("delivery_state", "TEXT NOT NULL DEFAULT 'pending'"),
            ("payload_ciphertext", "TEXT NOT NULL DEFAULT ''"),
            ("actor", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("allow_bot", "INTEGER NOT NULL DEFAULT 0"),
            ("max_attempts", "INTEGER NOT NULL DEFAULT 5"),
            ("next_attempt_at", "TEXT"),
            ("lease_owner", "TEXT"),
            ("lease_until", "TEXT"),
            ("dispatch_started_at", "TEXT"),
            ("last_attempt_at", "TEXT"),
            ("dead_letter_at", "TEXT"),
            ("reconciled_at", "TEXT"),
            ("reconciled_by", "TEXT"),
            ("reconciliation_note", "TEXT"),
            ("error_kind", "TEXT"),
            ("record_version", "INTEGER NOT NULL DEFAULT 1"),
        )
        for column, declaration in columns:
            cls._ensure_column(conn, "channel_outbox", column, declaration)
        now = utc_now()
        conn.execute(
            """
            UPDATE channel_outbox
            SET status='failed', delivery_state='uncertain',
                error_kind='legacy_inflight',
                last_error=COALESCE(last_error, 'legacy in-flight send requires reconciliation'),
                updated_at=?, record_version=record_version+1
            WHERE status='sending'
            """,
            (now,),
        )
        conn.execute(
            """
            UPDATE channel_outbox
            SET status='failed', delivery_state='dead_letter',
                error_kind='legacy_payload_missing', dead_letter_at=?,
                last_error=COALESCE(last_error, 'legacy queued item has no encrypted dispatch payload'),
                updated_at=?, record_version=record_version+1
            WHERE status='queued' AND payload_ciphertext=''
            """,
            (now, now),
        )
        conn.execute(
            "UPDATE channel_outbox SET delivery_state='confirmed' "
            "WHERE status='sent' AND delivery_state='pending'"
        )
        conn.execute(
            "UPDATE channel_outbox SET delivery_state='uncertain' "
            "WHERE status='failed' AND delivery_state='pending'"
        )
        conn.execute(
            "UPDATE channel_outbox SET next_attempt_at=created_at "
            "WHERE status='queued' AND next_attempt_at IS NULL"
        )
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_channel_outbox_due
                ON channel_outbox(status, next_attempt_at, lease_until, created_at);
            CREATE INDEX IF NOT EXISTS idx_channel_outbox_delivery
                ON channel_outbox(tenant_id, delivery_state, updated_at DESC);
            """
        )

    @classmethod
    def _apply_v11(cls, conn: sqlite3.Connection) -> None:
        cls._ensure_column(conn, "channel_outbox", "source_event_id", "TEXT")
        conn.execute(
            "UPDATE channel_outbox SET source_event_id=event_id WHERE source_event_id IS NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_outbox_source_event "
            "ON channel_outbox(source_event_id)"
        )

    @staticmethod
    def _apply_v12(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS release_policies (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                release_key TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                name TEXT NOT NULL,
                platform TEXT NOT NULL,
                store_id TEXT NOT NULL,
                mode TEXT NOT NULL CHECK(mode IN (
                    'shadow','assist','collaborative','automatic'
                )),
                traffic_percentage INTEGER NOT NULL
                    CHECK(traffic_percentage BETWEEN 0 AND 100),
                intent_allowlist_json TEXT NOT NULL,
                max_risk_level TEXT NOT NULL CHECK(max_risk_level IN ('low','medium')),
                require_sources INTEGER NOT NULL CHECK(require_sources IN (0,1)),
                allow_model_fallback INTEGER NOT NULL CHECK(allow_model_fallback IN (0,1)),
                min_replay_cases INTEGER NOT NULL CHECK(min_replay_cases > 0),
                max_replay_failure_rate REAL NOT NULL
                    CHECK(max_replay_failure_rate BETWEEN 0 AND 1),
                max_replay_severe_errors INTEGER NOT NULL
                    CHECK(max_replay_severe_errors >= 0),
                runtime_min_samples INTEGER NOT NULL CHECK(runtime_min_samples > 0),
                max_runtime_failure_rate REAL NOT NULL
                    CHECK(max_runtime_failure_rate BETWEEN 0 AND 1),
                max_runtime_severe_errors INTEGER NOT NULL
                    CHECK(max_runtime_severe_errors >= 0),
                rollout_salt TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'draft','evaluated','approved','active','paused','retired'
                )),
                latest_replay_run_id TEXT,
                evaluation_passed INTEGER CHECK(evaluation_passed IN (0,1)),
                evaluation_json TEXT,
                pause_reason TEXT,
                record_version INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL,
                approved_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                evaluated_at TEXT,
                approved_at TEXT,
                activated_at TEXT,
                paused_at TEXT,
                retired_at TEXT,
                UNIQUE(tenant_id, release_key, version)
            );
            CREATE INDEX IF NOT EXISTS idx_release_policies_scope
                ON release_policies(tenant_id, platform, store_id, status, updated_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_release_policies_one_active_scope
                ON release_policies(tenant_id, platform, store_id)
                WHERE status='active';

            CREATE TABLE IF NOT EXISTS release_replay_runs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                release_id TEXT NOT NULL REFERENCES release_policies(id) ON DELETE CASCADE,
                dataset_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running','passed','failed','error')),
                total_cases INTEGER NOT NULL DEFAULT 0,
                passed_cases INTEGER NOT NULL DEFAULT 0,
                failed_cases INTEGER NOT NULL DEFAULT 0,
                severe_errors INTEGER NOT NULL DEFAULT 0,
                failure_rate REAL NOT NULL DEFAULT 0,
                results_json TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                started_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_release_replay_runs_lookup
                ON release_replay_runs(tenant_id, release_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS release_observations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                release_id TEXT NOT NULL REFERENCES release_policies(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                assignment_bucket INTEGER NOT NULL
                    CHECK(assignment_bucket BETWEEN 0 AND 9999),
                selected INTEGER NOT NULL CHECK(selected IN (0,1)),
                intent TEXT,
                risk_level TEXT,
                requires_human INTEGER CHECK(requires_human IN (0,1)),
                source_count INTEGER,
                model_fallback INTEGER CHECK(model_fallback IN (0,1)),
                action TEXT NOT NULL CHECK(action IN (
                    'control','shadow','draft','send','handoff','blocked'
                )),
                violations_json TEXT NOT NULL DEFAULT '[]',
                severe INTEGER NOT NULL CHECK(severe IN (0,1)),
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id, release_id, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_release_observations_window
                ON release_observations(tenant_id, release_id, created_at DESC);
            """
        )

    @classmethod
    def _apply_v13(cls, conn: sqlite3.Connection) -> None:
        has_sop_runs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sop_runs'"
        ).fetchone()
        if has_sop_runs is None:
            # A forged migration marker must be reported by physical schema
            # validation, not hidden behind an ALTER TABLE error.
            return
        cls._ensure_column(conn, "sop_runs", "current_step_index", "INTEGER NOT NULL DEFAULT 0")
        cls._ensure_column(conn, "sop_runs", "record_version", "INTEGER NOT NULL DEFAULT 1")
        cls._ensure_column(conn, "sop_runs", "updated_at", "TEXT")
        cls._ensure_column(conn, "sop_runs", "last_error", "TEXT")
        conn.execute(
            "UPDATE sop_runs SET updated_at=COALESCE(updated_at, completed_at, started_at)"
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sop_step_runs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES sop_runs(id) ON DELETE CASCADE,
                step_id TEXT NOT NULL,
                step_index INTEGER NOT NULL CHECK(step_index >= 0),
                operation TEXT NOT NULL CHECK(operation IN (
                    'observe','clarify_if_missing','evaluate','propose','act'
                )),
                capability TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'pending','waiting_input','waiting_approval','running',
                    'succeeded','failed','uncertain','skipped',
                    'compensation_pending','compensating','compensated',
                    'compensation_failed','compensation_uncertain'
                )),
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                max_attempts INTEGER NOT NULL DEFAULT 1 CHECK(max_attempts BETWEEN 1 AND 10),
                input_hash TEXT,
                idempotency_key TEXT,
                result_json TEXT NOT NULL DEFAULT '{}',
                postcondition_met INTEGER NOT NULL DEFAULT 0 CHECK(postcondition_met IN (0,1)),
                error_code TEXT,
                compensation_tool TEXT,
                compensation_input_hash TEXT,
                compensation_idempotency_key TEXT,
                compensation_result_json TEXT NOT NULL DEFAULT '{}',
                compensation_error_code TEXT,
                compensation_attempt_count INTEGER NOT NULL DEFAULT 0
                    CHECK(compensation_attempt_count >= 0),
                requires_approval INTEGER NOT NULL DEFAULT 0 CHECK(requires_approval IN (0,1)),
                approved_by TEXT,
                approved_at TEXT,
                resolution_note TEXT,
                record_version INTEGER NOT NULL DEFAULT 1,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, step_id),
                UNIQUE(run_id, step_index)
            );
            CREATE INDEX IF NOT EXISTS idx_sop_step_runs_run
                ON sop_step_runs(tenant_id, run_id, step_index);
            CREATE INDEX IF NOT EXISTS idx_sop_step_runs_status
                ON sop_step_runs(tenant_id, status, updated_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sop_step_runs_idempotency
                ON sop_step_runs(tenant_id, idempotency_key)
                WHERE idempotency_key IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sop_step_runs_compensation_idempotency
                ON sop_step_runs(tenant_id, compensation_idempotency_key)
                WHERE compensation_idempotency_key IS NOT NULL;
            """
        )

        # V12 did not persist individual steps. Only active runs can be resumed
        # honestly; terminal runs remain historical without invented step results.
        active_runs = conn.execute(
            """
            SELECT r.id, r.tenant_id, v.dsl_json, r.updated_at
            FROM sop_runs r JOIN sop_versions v ON v.id=r.sop_version_id
            WHERE r.status='active'
            """
        ).fetchall()
        allowed_operations = {
            "observe", "clarify_if_missing", "evaluate", "propose", "act"
        }
        for run in active_runs:
            try:
                dsl = json.loads(run["dsl_json"] or "{}")
            except (TypeError, ValueError):
                continue
            for index, raw_step in enumerate(dsl.get("steps") or []):
                if not isinstance(raw_step, dict):
                    continue
                operation = next(
                    (name for name in allowed_operations if name in raw_step), None
                )

                if operation is None:
                    continue
                capability = str(raw_step.get(operation) or "").strip()
                if not capability:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sop_step_runs(
                        id, tenant_id, run_id, step_id, step_index, operation,
                        capability, status, attempt_count, max_attempts,
                        result_json, postcondition_met, compensation_tool,
                        requires_approval, record_version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, '{}', 0, ?, ?, 1, ?)
                    """,
                    (
                        f"sopstep-{uuid.uuid4().hex}",
                        run["tenant_id"],
                        run["id"],
                        f"step_{index + 1:02d}",
                        index,
                        operation,
                        capability,
                        max(1, min(int(raw_step.get("max_attempts", 1)), 10)),
                        raw_step.get("compensate_with"),
                        int(bool(raw_step.get("requires_approval", False))),
                        run["updated_at"] or utc_now(),
                    ),
                )

    @staticmethod
    def _apply_v14(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS competitive_monitors (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                subject_sku TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                undercut_threshold_percent TEXT NOT NULL DEFAULT '5.00'
                    CHECK(CAST(undercut_threshold_percent AS REAL) > 0),
                price_drop_threshold_percent TEXT NOT NULL DEFAULT '5.00'
                    CHECK(CAST(price_drop_threshold_percent AS REAL) > 0),
                stale_after_hours INTEGER NOT NULL DEFAULT 24
                    CHECK(stale_after_hours BETWEEN 1 AND 8760),
                include_estimates INTEGER NOT NULL DEFAULT 0
                    CHECK(include_estimates IN (0,1)),
                created_by TEXT NOT NULL,
                record_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, store_id, subject_sku)
            );
            CREATE INDEX IF NOT EXISTS idx_competitive_monitors_enabled
                ON competitive_monitors(tenant_id, enabled, updated_at DESC);

            CREATE TABLE IF NOT EXISTS competitive_alerts (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                monitor_id TEXT NOT NULL REFERENCES competitive_monitors(id) ON DELETE CASCADE,
                store_id TEXT NOT NULL,
                subject_sku TEXT NOT NULL,
                competitor_name TEXT NOT NULL,
                competitor_sku TEXT NOT NULL,
                alert_code TEXT NOT NULL CHECK(alert_code IN (
                    'competitor_undercut','competitor_price_drop','data_stale'
                )),
                severity TEXT NOT NULL CHECK(severity IN (
                    'info','attention','high','critical'
                )),
                status TEXT NOT NULL CHECK(status IN (
                    'open','acknowledged','resolved'
                )),
                value TEXT NOT NULL,
                threshold_value TEXT NOT NULL,
                observation_id TEXT,
                previous_observation_id TEXT,
                evidence_key TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count > 0),
                record_version INTEGER NOT NULL DEFAULT 1,
                first_detected_at TEXT NOT NULL,
                last_detected_at TEXT NOT NULL,
                acknowledged_by TEXT,
                acknowledged_at TEXT,
                acknowledgement_note TEXT,
                resolved_by TEXT,
                resolved_at TEXT,
                resolution_note TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, monitor_id, competitor_sku, alert_code)
            );
            CREATE INDEX IF NOT EXISTS idx_competitive_alerts_queue
                ON competitive_alerts(tenant_id, status, severity, last_detected_at DESC);
            CREATE INDEX IF NOT EXISTS idx_competitive_alerts_monitor
                ON competitive_alerts(tenant_id, monitor_id, competitor_sku, alert_code);
            """
        )

    @classmethod
    def _apply_v15(cls, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS context_snapshots (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                trace_id TEXT NOT NULL,
                stage TEXT NOT NULL CHECK(stage IN ('decision','generation')),
                sequence INTEGER NOT NULL CHECK(sequence >= 0),
                parent_snapshot_id TEXT REFERENCES context_snapshots(id),
                context_version TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                bundle_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                conflicts_json TEXT NOT NULL DEFAULT '[]',
                missing_json TEXT NOT NULL DEFAULT '[]',
                readiness TEXT NOT NULL CHECK(readiness IN (
                    'ready','clarification_required','handoff_required'
                )),
                checksum TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id, trace_id, stage, sequence)
            );
            CREATE INDEX IF NOT EXISTS idx_context_snapshots_session
                ON context_snapshots(tenant_id, session_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_context_snapshots_trace
                ON context_snapshots(tenant_id, trace_id, stage, sequence);
            """
        )
        messages_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        if messages_exists:
            cls._ensure_column(conn, "messages", "context_snapshot_id", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_context_snapshot "
                "ON messages(context_snapshot_id)"
            )

    @staticmethod
    def _apply_v16(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_invocations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                trace_id TEXT NOT NULL UNIQUE,
                user_message_id TEXT NOT NULL UNIQUE,
                assistant_message_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('running','completed')),
                response_json TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(tenant_id, client_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_agent_invocations_status
                ON agent_invocations(tenant_id, status, updated_at);

            CREATE TABLE IF NOT EXISTS channel_agent_jobs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                shop_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL REFERENCES channel_conversations(id) ON DELETE CASCADE,
                event_id TEXT NOT NULL REFERENCES channel_events(id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN (
                    'queued','running','retry','completed','blocked','dead_letter'
                )),
                stage TEXT NOT NULL CHECK(stage IN (
                    'queued','agent','agent_completed','materialize','done'
                )),
                release_id TEXT,
                release_mode TEXT CHECK(release_mode IS NULL OR release_mode IN (
                    'shadow','assist','collaborative','automatic'
                )),
                assignment_bucket INTEGER CHECK(
                    assignment_bucket IS NULL OR assignment_bucket BETWEEN 0 AND 9999
                ),
                action TEXT CHECK(action IS NULL OR action IN (
                    'control','shadow','draft','send','handoff','blocked','disabled'
                )),
                agent_invocation_id TEXT REFERENCES agent_invocations(id) ON DELETE SET NULL,
                assistant_message_id TEXT,
                context_snapshot_id TEXT,
                release_observation_id TEXT,
                reply_draft_id TEXT,
                outbox_id TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts > 0),
                next_attempt_at TEXT,
                lease_owner TEXT,
                lease_until TEXT,
                last_error TEXT,
                error_kind TEXT,
                record_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE(tenant_id, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_channel_agent_jobs_due
                ON channel_agent_jobs(status, next_attempt_at, lease_until, created_at);
            CREATE INDEX IF NOT EXISTS idx_channel_agent_jobs_tenant
                ON channel_agent_jobs(tenant_id, status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_channel_agent_jobs_conversation
                ON channel_agent_jobs(tenant_id, conversation_id, created_at DESC);
            """
        )

    @classmethod
    def _apply_v17(cls, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS competitive_entity_matches (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                subject_sku TEXT NOT NULL,
                competitor_name TEXT NOT NULL,
                competitor_sku TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN (
                    'authorized_api','licensed_provider','manual','file_import','virtual'
                )),
                source_ref TEXT NOT NULL,
                source_id TEXT NOT NULL,
                is_estimate INTEGER NOT NULL DEFAULT 1 CHECK(is_estimate IN (0, 1)),
                observed_at TEXT NOT NULL,
                subject_identity_json TEXT NOT NULL,
                competitor_identity_json TEXT NOT NULL,
                comparison_keys_json TEXT NOT NULL DEFAULT '[]',
                score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
                matched_fields_json TEXT NOT NULL DEFAULT '[]',
                conflicts_json TEXT NOT NULL DEFAULT '[]',
                missing_fields_json TEXT NOT NULL DEFAULT '[]',
                recommended_status TEXT NOT NULL CHECK(recommended_status IN (
                    'approved','pending','rejected'
                )),
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                    'pending','approved','rejected'
                )),
                payload_hash TEXT NOT NULL,
                record_version INTEGER NOT NULL DEFAULT 1,
                reviewed_by TEXT,
                reviewed_at TEXT,
                review_note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, connector_id, source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_competitive_matches_queue
                ON competitive_entity_matches(tenant_id, status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_competitive_matches_scope
                ON competitive_entity_matches(
                    tenant_id, store_id, subject_sku, competitor_sku, status
                );

            CREATE TABLE IF NOT EXISTS competitive_match_decisions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                match_id TEXT NOT NULL REFERENCES competitive_entity_matches(id)
                    ON DELETE CASCADE,
                from_status TEXT NOT NULL CHECK(from_status IN (
                    'pending','approved','rejected'
                )),
                to_status TEXT NOT NULL CHECK(to_status IN (
                    'approved','rejected'
                )),
                match_record_version INTEGER NOT NULL CHECK(match_record_version > 1),
                actor TEXT NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_competitive_match_decisions_match
                ON competitive_match_decisions(tenant_id, match_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS competitive_signals (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                match_id TEXT NOT NULL REFERENCES competitive_entity_matches(id)
                    ON DELETE CASCADE,
                connector_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                subject_sku TEXT NOT NULL,
                competitor_name TEXT NOT NULL,
                competitor_sku TEXT NOT NULL,
                entity_role TEXT NOT NULL CHECK(entity_role IN ('subject','competitor')),
                signal_type TEXT NOT NULL CHECK(signal_type IN (
                    'product_claim','review_summary'
                )),
                aspect TEXT NOT NULL,
                summary_redacted TEXT NOT NULL,
                sample_size INTEGER CHECK(sample_size IS NULL OR sample_size >= 5),
                positive_count INTEGER CHECK(positive_count IS NULL OR positive_count >= 0),
                negative_count INTEGER CHECK(negative_count IS NULL OR negative_count >= 0),
                source_type TEXT NOT NULL CHECK(source_type IN (
                    'authorized_api','licensed_provider','manual','file_import','virtual'
                )),
                source_ref TEXT NOT NULL,
                source_id TEXT NOT NULL,
                is_estimate INTEGER NOT NULL DEFAULT 1 CHECK(is_estimate IN (0, 1)),
                observed_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id, connector_id, source_id)
            );
            CREATE INDEX IF NOT EXISTS idx_competitive_signals_scope
                ON competitive_signals(
                    tenant_id, store_id, subject_sku, signal_type, observed_at DESC
                );
            CREATE INDEX IF NOT EXISTS idx_competitive_signals_match
                ON competitive_signals(tenant_id, match_id, observed_at DESC);
            """
        )
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "competitor_observations" in tables:
            cls._ensure_column(conn, "competitor_observations", "entity_match_id", "TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_competitor_observations_match "
                "ON competitor_observations(tenant_id, entity_match_id)"
            )
        if "competitive_monitors" in tables:
            cls._ensure_column(
                conn,
                "competitive_monitors",
                "require_approved_match",
                "INTEGER NOT NULL DEFAULT 0 CHECK(require_approved_match IN (0, 1))",
            )

    @classmethod
    def _apply_v18(cls, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS evaluation_suites (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                suite_key TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version > 0),
                previous_suite_id TEXT REFERENCES evaluation_suites(id) ON DELETE SET NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL CHECK(source_type IN (
                    'manual','file_import','customer_labeled','synthetic'
                )),
                source_ref TEXT NOT NULL,
                deidentified INTEGER NOT NULL CHECK(deidentified IN (0,1)),
                required_scenarios_json TEXT NOT NULL DEFAULT '[]',
                thresholds_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('draft','frozen','retired')),
                dataset_hash TEXT,
                case_count INTEGER NOT NULL DEFAULT 0 CHECK(case_count >= 0),
                latest_run_id TEXT,
                record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version > 0),
                created_by TEXT NOT NULL,
                frozen_by TEXT,
                retired_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                frozen_at TEXT,
                retired_at TEXT,
                UNIQUE(tenant_id, suite_key, version)
            );
            CREATE INDEX IF NOT EXISTS idx_evaluation_suites_scope
                ON evaluation_suites(tenant_id, status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS evaluation_cases (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                suite_id TEXT NOT NULL REFERENCES evaluation_suites(id) ON DELETE CASCADE,
                case_key TEXT NOT NULL,
                scenario TEXT NOT NULL,
                source_ref TEXT NOT NULL DEFAULT '',
                turns_json TEXT NOT NULL,
                case_hash TEXT NOT NULL,
                input_redacted INTEGER NOT NULL DEFAULT 0 CHECK(input_redacted IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, suite_id, case_key)
            );
            CREATE INDEX IF NOT EXISTS idx_evaluation_cases_suite
                ON evaluation_cases(tenant_id, suite_id, scenario, case_key);

            CREATE TABLE IF NOT EXISTS evaluation_runs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                suite_id TEXT NOT NULL REFERENCES evaluation_suites(id) ON DELETE CASCADE,
                release_id TEXT REFERENCES release_policies(id) ON DELETE SET NULL,
                baseline_run_id TEXT REFERENCES evaluation_runs(id) ON DELETE SET NULL,
                run_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('running','passed','failed','error')),
                runner_version TEXT NOT NULL,
                dataset_hash TEXT NOT NULL,
                total_cases INTEGER NOT NULL DEFAULT 0,
                passed_cases INTEGER NOT NULL DEFAULT 0,
                failed_cases INTEGER NOT NULL DEFAULT 0,
                severe_failures INTEGER NOT NULL DEFAULT 0,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                gate_json TEXT NOT NULL DEFAULT '{}',
                release_gate_applied INTEGER CHECK(release_gate_applied IN (0,1)),
                release_gate_error TEXT,
                error_code TEXT,
                started_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(tenant_id, suite_id, run_key)
            );
            CREATE INDEX IF NOT EXISTS idx_evaluation_runs_scope
                ON evaluation_runs(tenant_id, suite_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_evaluation_runs_release
                ON evaluation_runs(tenant_id, release_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS evaluation_case_results (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
                case_id TEXT NOT NULL REFERENCES evaluation_cases(id) ON DELETE CASCADE,
                case_key TEXT NOT NULL,
                scenario TEXT NOT NULL,
                passed INTEGER NOT NULL CHECK(passed IN (0,1)),
                severe INTEGER NOT NULL CHECK(severe IN (0,1)),
                passed_turns INTEGER NOT NULL DEFAULT 0,
                total_turns INTEGER NOT NULL DEFAULT 0,
                violations_json TEXT NOT NULL DEFAULT '[]',
                actual_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id, run_id, case_id)
            );
            CREATE INDEX IF NOT EXISTS idx_evaluation_case_results_run
                ON evaluation_case_results(tenant_id, run_id, scenario, passed);
            """
        )
        release_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_policies'"
        ).fetchone()
        if release_table is not None:
            cls._ensure_column(conn, "release_policies", "latest_evaluation_run_id", "TEXT")

    @classmethod
    def _apply_v19(cls, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS handoff_queues (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                queue_key TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK(status IN ('active','inactive')),
                default_priority TEXT NOT NULL CHECK(default_priority IN (
                    'low','normal','high','urgent'
                )),
                first_response_sla_minutes INTEGER NOT NULL
                    CHECK(first_response_sla_minutes BETWEEN 1 AND 10080),
                resolution_sla_minutes INTEGER NOT NULL
                    CHECK(resolution_sla_minutes BETWEEN 1 AND 43200),
                max_active_per_operator INTEGER NOT NULL
                    CHECK(max_active_per_operator BETWEEN 1 AND 100),
                escalation_queue_id TEXT REFERENCES handoff_queues(id) ON DELETE SET NULL,
                match_reasons_json TEXT NOT NULL DEFAULT '[]',
                match_intents_json TEXT NOT NULL DEFAULT '[]',
                match_risk_levels_json TEXT NOT NULL DEFAULT '[]',
                routing_order INTEGER NOT NULL DEFAULT 100 CHECK(routing_order >= 0),
                record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version > 0),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, queue_key),
                CHECK(resolution_sla_minutes >= first_response_sla_minutes)
            );
            CREATE INDEX IF NOT EXISTS idx_handoff_queues_scope
                ON handoff_queues(tenant_id, status, routing_order, queue_key);

            CREATE TABLE IF NOT EXISTS handoff_task_events (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                handoff_id TEXT NOT NULL REFERENCES handoff_tasks(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'created','migrated','claimed','transitioned','reassigned',
                    'escalated','note_added'
                )),
                from_status TEXT,
                to_status TEXT,
                from_queue_id TEXT REFERENCES handoff_queues(id) ON DELETE SET NULL,
                to_queue_id TEXT REFERENCES handoff_queues(id) ON DELETE SET NULL,
                from_assignee TEXT,
                to_assignee TEXT,
                task_version INTEGER NOT NULL CHECK(task_version > 0),
                actor TEXT NOT NULL,
                note_redacted TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(tenant_id, handoff_id, task_version)
            );
            CREATE INDEX IF NOT EXISTS idx_handoff_events_task
                ON handoff_task_events(tenant_id, handoff_id, task_version DESC);
            """
        )

        handoff_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='handoff_tasks'"
        ).fetchone()
        if handoff_table is None:
            return
        cls._ensure_column(
            conn,
            "handoff_tasks",
            "queue_id",
            "TEXT REFERENCES handoff_queues(id) ON DELETE SET NULL",
        )
        cls._ensure_column(
            conn,
            "handoff_tasks",
            "priority",
            "TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('low','normal','high','urgent'))",
        )
        cls._ensure_column(conn, "handoff_tasks", "sla_first_response_at", "TEXT")
        cls._ensure_column(conn, "handoff_tasks", "sla_resolution_at", "TEXT")
        cls._ensure_column(conn, "handoff_tasks", "acknowledged_at", "TEXT")
        cls._ensure_column(conn, "handoff_tasks", "started_at", "TEXT")
        cls._ensure_column(conn, "handoff_tasks", "review_started_at", "TEXT")
        cls._ensure_column(conn, "handoff_tasks", "escalated_at", "TEXT")
        cls._ensure_column(
            conn,
            "handoff_tasks",
            "escalation_level",
            "INTEGER NOT NULL DEFAULT 0 CHECK(escalation_level BETWEEN 0 AND 2)",
        )
        cls._ensure_column(conn, "handoff_tasks", "escalation_reason", "TEXT")
        now = utc_now()
        tenant_rows = conn.execute(
            "SELECT DISTINCT tenant_id FROM handoff_tasks WHERE tenant_id IS NOT NULL"
        ).fetchall()
        for row in tenant_rows:
            tenant_id = str(row[0])
            queue_id = f"queue-legacy-{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT OR IGNORE INTO handoff_queues(
                    id, tenant_id, queue_key, name, description, status,
                    default_priority, first_response_sla_minutes,
                    resolution_sla_minutes, max_active_per_operator,
                    escalation_queue_id, match_reasons_json, match_intents_json,
                    match_risk_levels_json, routing_order, record_version,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, 'general', '通用接管队列', '迁移及默认人工接管队列',
                          'active', 'normal', 30, 1440, 20, NULL, '[]', '[]', '[]',
                          999, 1, 'schema-v19', ?, ?)
                """,
                (queue_id, tenant_id, now, now),
            )
            resolved_queue = conn.execute(
                "SELECT id FROM handoff_queues WHERE tenant_id=? AND queue_key='general'",
                (tenant_id,),
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE handoff_tasks
                SET queue_id=?,
                    sla_first_response_at=COALESCE(sla_first_response_at, deadline_at),
                    sla_resolution_at=COALESCE(sla_resolution_at, deadline_at)
                WHERE tenant_id=? AND queue_id IS NULL
                """,
                (resolved_queue, tenant_id),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO handoff_task_events(
                id, tenant_id, handoff_id, event_type, from_status, to_status,
                from_queue_id, to_queue_id, from_assignee, to_assignee,
                task_version, actor, note_redacted, created_at
            )
            SELECT 'event-' || lower(hex(randomblob(16))), tenant_id, id, 'migrated',
                   NULL, status, NULL, queue_id, NULL, assigned_to,
                   version, 'schema-v19', 'schema v19 migration', updated_at
            FROM handoff_tasks
            """
        )
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_handoffs_queue_work
                ON handoff_tasks(tenant_id, queue_id, status, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_handoffs_assignee_work
                ON handoff_tasks(tenant_id, assigned_to, status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_handoffs_sla
                ON handoff_tasks(tenant_id, status, sla_first_response_at, sla_resolution_at);
            """
        )

    @classmethod
    def _apply_v20(cls, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS handoff_operator_profiles (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                admin_id TEXT NOT NULL REFERENCES api_clients(id) ON DELETE CASCADE,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','inactive')),
                presence TEXT NOT NULL CHECK(presence IN ('available','away','offline')),
                max_active_tasks INTEGER NOT NULL CHECK(max_active_tasks BETWEEN 1 AND 100),
                skills_json TEXT NOT NULL DEFAULT '[]',
                record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version > 0),
                presence_updated_at TEXT NOT NULL,
                presence_expires_at TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, admin_id)
            );
            CREATE INDEX IF NOT EXISTS idx_handoff_operator_presence
                ON handoff_operator_profiles(tenant_id, status, presence, presence_expires_at);

            CREATE TABLE IF NOT EXISTS handoff_operator_queue_memberships (
                operator_profile_id TEXT NOT NULL
                    REFERENCES handoff_operator_profiles(id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL,
                queue_id TEXT NOT NULL REFERENCES handoff_queues(id) ON DELETE CASCADE,
                skill_level INTEGER NOT NULL DEFAULT 3 CHECK(skill_level BETWEEN 1 AND 5),
                is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(operator_profile_id, queue_id)
            );
            CREATE INDEX IF NOT EXISTS idx_handoff_operator_queue
                ON handoff_operator_queue_memberships(
                    tenant_id, queue_id, is_primary DESC, skill_level DESC
                );

            CREATE TRIGGER IF NOT EXISTS trg_handoff_operator_membership_tenant_insert
            BEFORE INSERT ON handoff_operator_queue_memberships
            WHEN (SELECT tenant_id FROM handoff_operator_profiles
                  WHERE id=NEW.operator_profile_id) <> NEW.tenant_id
              OR (SELECT tenant_id FROM handoff_queues WHERE id=NEW.queue_id) <> NEW.tenant_id
            BEGIN
                SELECT RAISE(ABORT, 'handoff operator membership tenant mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_handoff_operator_membership_tenant_update
            BEFORE UPDATE ON handoff_operator_queue_memberships
            WHEN (SELECT tenant_id FROM handoff_operator_profiles
                  WHERE id=NEW.operator_profile_id) <> NEW.tenant_id
              OR (SELECT tenant_id FROM handoff_queues WHERE id=NEW.queue_id) <> NEW.tenant_id
            BEGIN
                SELECT RAISE(ABORT, 'handoff operator membership tenant mismatch');
            END;
            """
        )
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(hours=8)).isoformat()
        physical_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not {"api_clients", "handoff_queues"} <= physical_tables:
            return
        conn.execute(
            """
            INSERT OR IGNORE INTO handoff_operator_profiles(
                id, tenant_id, admin_id, display_name, status, presence,
                max_active_tasks, skills_json, record_version,
                presence_updated_at, presence_expires_at, created_by,
                created_at, updated_at
            )
            SELECT 'operator-' || lower(hex(randomblob(16))), tenant_id, id, name,
                   'active', 'available', 20, '[]', 1, ?, ?, 'schema-v20', ?, ?
            FROM api_clients
            WHERE role='admin' AND status='active'
            """,
            (now, expires_at, now, now),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO handoff_operator_queue_memberships(
                operator_profile_id, tenant_id, queue_id, skill_level,
                is_primary, created_at, updated_at
            )
            SELECT p.id, p.tenant_id, q.id, 3,
                   CASE WHEN q.queue_key='general' THEN 1 ELSE 0 END, ?, ?
            FROM handoff_operator_profiles p
            JOIN handoff_queues q ON q.tenant_id=p.tenant_id AND q.status='active'
            """,
            (now, now),
        )

    @classmethod
    def _apply_v21(cls, conn: sqlite3.Connection) -> None:
        cls._ensure_column(
            conn,
            "handoff_operator_profiles",
            "dispatch_mode",
            "TEXT NOT NULL DEFAULT 'automatic' CHECK(dispatch_mode IN ('automatic','manual'))",
        )
        cls._ensure_column(
            conn,
            "handoff_operator_profiles",
            "schedule_mode",
            "TEXT NOT NULL DEFAULT 'unrestricted' CHECK(schedule_mode IN ('unrestricted','scheduled'))",
        )
        cls._ensure_column(
            conn,
            "handoff_operator_profiles",
            "presence_version",
            "INTEGER NOT NULL DEFAULT 1 CHECK(presence_version > 0)",
        )
        cls._ensure_column(
            conn,
            "handoff_operator_profiles",
            "presence_session_id",
            "TEXT",
        )
        cls._ensure_column(
            conn,
            "handoff_operator_profiles",
            "presence_sequence",
            "INTEGER NOT NULL DEFAULT 0 CHECK(presence_sequence >= 0)",
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS handoff_operator_shifts (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                operator_profile_id TEXT NOT NULL
                    REFERENCES handoff_operator_profiles(id) ON DELETE CASCADE,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('scheduled','cancelled')),
                record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version > 0),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(ends_at > starts_at)
            );
            CREATE INDEX IF NOT EXISTS idx_handoff_operator_shifts_window
                ON handoff_operator_shifts(
                    tenant_id, operator_profile_id, status, starts_at, ends_at
                );

            CREATE TRIGGER IF NOT EXISTS trg_handoff_operator_shift_tenant_insert
            BEFORE INSERT ON handoff_operator_shifts
            WHEN (SELECT tenant_id FROM handoff_operator_profiles
                  WHERE id=NEW.operator_profile_id) <> NEW.tenant_id
            BEGIN
                SELECT RAISE(ABORT, 'handoff operator shift tenant mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_handoff_operator_shift_tenant_update
            BEFORE UPDATE ON handoff_operator_shifts
            WHEN (SELECT tenant_id FROM handoff_operator_profiles
                  WHERE id=NEW.operator_profile_id) <> NEW.tenant_id
            BEGIN
                SELECT RAISE(ABORT, 'handoff operator shift tenant mismatch');
            END;

            CREATE TABLE IF NOT EXISTS handoff_dispatch_jobs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                handoff_id TEXT NOT NULL REFERENCES handoff_tasks(id) ON DELETE CASCADE,
                queue_id TEXT NOT NULL REFERENCES handoff_queues(id) ON DELETE CASCADE,
                priority TEXT NOT NULL CHECK(priority IN ('low','normal','high','urgent')),
                status TEXT NOT NULL CHECK(
                    status IN ('pending','leased','waiting','assigned','cancelled','failed')
                ),
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                available_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                assigned_to TEXT,
                last_error TEXT,
                record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version > 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(tenant_id, handoff_id)
            );
            CREATE INDEX IF NOT EXISTS idx_handoff_dispatch_claim
                ON handoff_dispatch_jobs(status, available_at, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_handoff_dispatch_tenant
                ON handoff_dispatch_jobs(tenant_id, status, updated_at DESC);

            CREATE TRIGGER IF NOT EXISTS trg_handoff_dispatch_job_tenant_insert
            BEFORE INSERT ON handoff_dispatch_jobs
            WHEN (SELECT tenant_id FROM handoff_tasks WHERE id=NEW.handoff_id) <> NEW.tenant_id
              OR (SELECT tenant_id FROM handoff_queues WHERE id=NEW.queue_id) <> NEW.tenant_id
            BEGIN
                SELECT RAISE(ABORT, 'handoff dispatch job tenant mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_handoff_dispatch_job_tenant_update
            BEFORE UPDATE ON handoff_dispatch_jobs
            WHEN (SELECT tenant_id FROM handoff_tasks WHERE id=NEW.handoff_id) <> NEW.tenant_id
              OR (SELECT tenant_id FROM handoff_queues WHERE id=NEW.queue_id) <> NEW.tenant_id
            BEGIN
                SELECT RAISE(ABORT, 'handoff dispatch job tenant mismatch');
            END;

            CREATE TABLE IF NOT EXISTS handoff_dispatch_alerts (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                handoff_id TEXT NOT NULL REFERENCES handoff_tasks(id) ON DELETE CASCADE,
                queue_id TEXT NOT NULL REFERENCES handoff_queues(id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN ('open','acknowledged','resolved')),
                reason TEXT NOT NULL CHECK(reason IN ('no_available_operator','dispatch_error')),
                occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK(occurrence_count > 0),
                detail_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                acknowledged_by TEXT,
                acknowledged_at TEXT,
                resolved_at TEXT,
                record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version > 0),
                UNIQUE(tenant_id, handoff_id)
            );
            CREATE INDEX IF NOT EXISTS idx_handoff_dispatch_alerts_open
                ON handoff_dispatch_alerts(tenant_id, status, last_seen_at DESC);

            CREATE TRIGGER IF NOT EXISTS trg_handoff_dispatch_alert_tenant_insert
            BEFORE INSERT ON handoff_dispatch_alerts
            WHEN (SELECT tenant_id FROM handoff_tasks WHERE id=NEW.handoff_id) <> NEW.tenant_id
              OR (SELECT tenant_id FROM handoff_queues WHERE id=NEW.queue_id) <> NEW.tenant_id
            BEGIN
                SELECT RAISE(ABORT, 'handoff dispatch alert tenant mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_handoff_dispatch_alert_tenant_update
            BEFORE UPDATE ON handoff_dispatch_alerts
            WHEN (SELECT tenant_id FROM handoff_tasks WHERE id=NEW.handoff_id) <> NEW.tenant_id
              OR (SELECT tenant_id FROM handoff_queues WHERE id=NEW.queue_id) <> NEW.tenant_id
            BEGIN
                SELECT RAISE(ABORT, 'handoff dispatch alert tenant mismatch');
            END;
            """
        )
        physical_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if {"handoff_tasks", "handoff_queues"} <= physical_tables:
            conn.execute(
                """
                INSERT OR IGNORE INTO handoff_dispatch_jobs(
                    id, tenant_id, handoff_id, queue_id, priority, status,
                    attempt_count, available_at, record_version, created_at, updated_at
                )
                SELECT 'dispatch-' || lower(hex(randomblob(16))), tenant_id, id, queue_id,
                       priority, 'pending', 0, updated_at, 1, created_at, updated_at
                FROM handoff_tasks
                WHERE status='proposed' AND assigned_to IS NULL AND queue_id IS NOT NULL
                """
            )

    @classmethod
    def _apply_v22(cls, conn: sqlite3.Connection) -> None:
        sessions_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
        if sessions_exists is None:
            return
        cls._ensure_column(
            conn,
            "sessions",
            "source_type",
            "TEXT NOT NULL DEFAULT 'api'",
        )
        cls._ensure_column(conn, "sessions", "source_reference", "TEXT")
        conn.executescript(
            """
            UPDATE sessions
            SET source_type=CASE
                WHEN external_session_id LIKE 'virtual-%' THEN 'simulation'
                WHEN external_session_id LIKE 'evaluation:%' THEN 'evaluation'
                WHEN external_session_id LIKE 'replay:%' THEN 'evaluation'
                WHEN external_session_id LIKE 'taobao:%' THEN 'channel'
                ELSE source_type
            END;
            UPDATE sessions
            SET source_reference='legacy-virtual'
            WHERE source_type='simulation' AND source_reference IS NULL;
            CREATE INDEX IF NOT EXISTS idx_sessions_tenant_source_time
                ON sessions(tenant_id, source_type, last_seen_at DESC);
            """
        )

    @staticmethod
    def _apply_v23(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS marketing_campaign_metrics (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                campaign_id TEXT NOT NULL,
                metric_date TEXT NOT NULL,
                campaign_name TEXT NOT NULL,
                channel TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','paused','ended')),
                spend TEXT NOT NULL,
                attributed_revenue TEXT NOT NULL,
                attributed_orders INTEGER NOT NULL,
                impressions INTEGER NOT NULL,
                clicks INTEGER NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN ('virtual','file_import')),
                source_id TEXT,
                source_updated_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, connector_id, store_id, campaign_id, metric_date)
            );
            CREATE INDEX IF NOT EXISTS idx_marketing_metrics_tenant_store_date
                ON marketing_campaign_metrics(tenant_id, store_id, metric_date DESC);

            CREATE TABLE IF NOT EXISTS marketing_content_drafts (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                draft_key TEXT NOT NULL,
                store_id TEXT NOT NULL,
                content_type TEXT NOT NULL CHECK(content_type IN ('product_copy','campaign_copy','social_post')),
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                sku_ids_json TEXT NOT NULL,
                declared_prices_json TEXT NOT NULL,
                fact_check_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('draft','review','approved','rejected')),
                source_type TEXT NOT NULL CHECK(source_type IN ('manual','virtual')),
                source_id TEXT,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, draft_key)
            );
            CREATE INDEX IF NOT EXISTS idx_marketing_drafts_tenant_store_updated
                ON marketing_content_drafts(tenant_id, store_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS operating_expenses (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                expense_key TEXT NOT NULL,
                occurred_on TEXT NOT NULL,
                category TEXT NOT NULL CHECK(category IN ('product_cost','advertising','platform_fee','logistics','fulfillment','refund','other')),
                amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN ('virtual','file_import')),
                source_id TEXT,
                source_updated_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, connector_id, store_id, expense_key)
            );
            CREATE INDEX IF NOT EXISTS idx_operating_expenses_tenant_store_date
                ON operating_expenses(tenant_id, store_id, occurred_on DESC);

            CREATE TABLE IF NOT EXISTS settlement_statements (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                store_id TEXT NOT NULL,
                statement_key TEXT NOT NULL,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                gross_sales TEXT NOT NULL,
                refund_amount TEXT NOT NULL,
                fee_amount TEXT NOT NULL,
                settlement_amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN ('virtual','file_import')),
                source_id TEXT,
                source_updated_at TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, connector_id, store_id, statement_key)
            );
            CREATE INDEX IF NOT EXISTS idx_settlement_statements_tenant_store_period
                ON settlement_statements(tenant_id, store_id, period_end DESC);

            CREATE TABLE IF NOT EXISTS reconciliation_tasks (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                statement_id TEXT NOT NULL REFERENCES settlement_statements(id) ON DELETE CASCADE,
                store_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('open','reviewing','resolved','ignored')),
                expected_settlement TEXT NOT NULL,
                reported_settlement TEXT NOT NULL,
                difference_amount TEXT NOT NULL,
                tolerance_amount TEXT NOT NULL,
                note TEXT,
                record_version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, statement_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reconciliation_tasks_tenant_status
                ON reconciliation_tasks(tenant_id, status, updated_at DESC);
            """
        )

    @staticmethod
    def _apply_v24(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS staged_rollouts (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                subject_type TEXT NOT NULL CHECK(subject_type IN ('knowledge','sop')),
                subject_key TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                baseline_id TEXT,
                traffic_percentage INTEGER NOT NULL
                    CHECK(traffic_percentage BETWEEN 1 AND 100),
                rollout_salt TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','completed','rolled_back')),
                note TEXT,
                record_version INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL,
                completed_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_staged_rollouts_one_active
                ON staged_rollouts(tenant_id, subject_type, subject_key)
                WHERE status='active';
            CREATE INDEX IF NOT EXISTS idx_staged_rollouts_scope
                ON staged_rollouts(tenant_id, subject_type, status, updated_at DESC);
            """
        )

    @classmethod
    def _apply_v25(cls, conn: sqlite3.Connection) -> None:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='release_policies'"
        ).fetchone()
        if exists is None:
            # A drifted database without the table is rejected by
            # _validate_schema right after the migration pass.
            return
        cls._ensure_column(conn, "release_policies", "night_window_start_utc", "TEXT")
        cls._ensure_column(conn, "release_policies", "night_window_end_utc", "TEXT")
        cls._ensure_column(conn, "release_policies", "night_mode", "TEXT")
        cls._ensure_column(conn, "release_policies", "sop_allowlist_json", "TEXT")

    @staticmethod
    def _apply_v25(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ops_operation_records (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                dataset_key TEXT NOT NULL,
                store_id TEXT NOT NULL,
                record_date TEXT NOT NULL,
                channel TEXT NOT NULL,
                visitors INTEGER NOT NULL CHECK(visitors >= 0),
                orders INTEGER NOT NULL CHECK(orders >= 0),
                sales_amount TEXT NOT NULL,
                ad_spend TEXT NOT NULL,
                source_format TEXT NOT NULL CHECK(source_format IN ('csv','json','form')),
                source_id TEXT,
                payload_hash TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, dataset_key, record_date, channel)
            );
            CREATE INDEX IF NOT EXISTS idx_ops_operation_records_scope
                ON ops_operation_records(tenant_id, store_id, record_date DESC);
            """
        )

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        required = {
            "sessions": {"tenant_id", "source_type", "source_reference"},
            "marketing_campaign_metrics": {
                "tenant_id", "connector_id", "store_id", "campaign_id", "metric_date",
                "spend", "attributed_revenue", "source_type", "payload_hash", "version",
            },
            "marketing_content_drafts": {
                "tenant_id", "draft_key", "store_id", "fact_check_json", "status", "version",
            },
            "operating_expenses": {
                "tenant_id", "connector_id", "store_id", "expense_key", "category", "amount",
                "source_type", "payload_hash", "version",
            },
            "settlement_statements": {
                "tenant_id", "connector_id", "store_id", "statement_key", "settlement_amount",
                "source_type", "payload_hash", "version",
            },
            "reconciliation_tasks": {
                "tenant_id", "statement_id", "status", "difference_amount", "record_version",
            },
            "knowledge": {"knowledge_key", "layer", "review_status", "record_version"},
            "ops_operation_records": {
                "tenant_id", "dataset_key", "store_id", "record_date", "channel",
                "visitors", "orders", "sales_amount", "ad_spend", "source_format",
                "payload_hash", "version",
            },
            "staged_rollouts": {
                "tenant_id", "subject_type", "subject_key", "candidate_id",
                "traffic_percentage", "rollout_salt", "status", "record_version",
            },
            "release_policies": {
                "tenant_id", "release_key", "mode", "traffic_percentage",
                "rollout_salt", "night_window_start_utc", "night_window_end_utc",
                "night_mode", "sop_allowlist_json",
            },
            "competitor_observations": {
                "tenant_id",
                "connector_id",
                "store_id",
                "subject_sku",
                "competitor_sku",
                "payload_hash",
                "entity_match_id",
            },
            "sop_definitions": {"tenant_id", "sop_key", "current_active_version"},
            "sop_versions": {"definition_id", "version", "dsl_json", "status"},
            "sop_runs": {
                "tenant_id",
                "sop_version_id",
                "status",
                "current_step_index",
                "record_version",
                "updated_at",
                "last_error",
            },
            "sop_step_runs": {
                "tenant_id",
                "run_id",
                "step_id",
                "step_index",
                "operation",
                "capability",
                "status",
                "attempt_count",
                "max_attempts",
                "input_hash",
                "idempotency_key",
                "result_json",
                "postcondition_met",
                "compensation_tool",
                "compensation_input_hash",
                "compensation_idempotency_key",
                "compensation_result_json",
                "compensation_error_code",
                "compensation_attempt_count",
                "requires_approval",
                "record_version",
            },
            "competitive_monitors": {
                "tenant_id",
                "store_id",
                "subject_sku",
                "enabled",
                "undercut_threshold_percent",
                "price_drop_threshold_percent",
                "stale_after_hours",
                "include_estimates",
                "require_approved_match",
                "record_version",
            },
            "competitive_entity_matches": {
                "tenant_id",
                "connector_id",
                "store_id",
                "subject_sku",
                "competitor_sku",
                "source_id",
                "score",
                "recommended_status",
                "status",
                "payload_hash",
                "record_version",
            },
            "competitive_match_decisions": {
                "tenant_id",
                "match_id",
                "from_status",
                "to_status",
                "match_record_version",
                "actor",
                "note",
            },
            "competitive_signals": {
                "tenant_id",
                "match_id",
                "entity_role",
                "signal_type",
                "aspect",
                "summary_redacted",
                "sample_size",
                "source_id",
                "payload_hash",
            },
            "competitive_alerts": {
                "tenant_id",
                "monitor_id",
                "store_id",
                "subject_sku",
                "competitor_sku",
                "alert_code",
                "severity",
                "status",
                "evidence_key",
                "occurrence_count",
                "record_version",
            },
            "context_snapshots": {
                "tenant_id",
                "session_id",
                "trace_id",
                "stage",
                "sequence",
                "parent_snapshot_id",
                "context_version",
                "request_hash",
                "bundle_json",
                "evidence_json",
                "conflicts_json",
                "missing_json",
                "readiness",
                "checksum",
            },
            "agent_invocations": {
                "tenant_id",
                "client_id",
                "session_id",
                "idempotency_key",
                "request_hash",
                "trace_id",
                "assistant_message_id",
                "status",
                "response_json",
                "attempt_count",
            },
            "channel_agent_jobs": {
                "tenant_id",
                "platform",
                "shop_id",
                "conversation_id",
                "event_id",
                "status",
                "stage",
                "release_id",
                "release_mode",
                "action",
                "agent_invocation_id",
                "reply_draft_id",
                "outbox_id",
                "attempt_count",
                "max_attempts",
                "next_attempt_at",
                "lease_owner",
                "lease_until",
                "record_version",
            },
            "messages": {"tenant_id", "client_id", "redacted", "context_snapshot_id"},
            "qa_results": {"tenant_id", "issues_json", "review_status", "record_version"},
            "channel_reply_drafts": {"outbox_id", "record_version", "status"},
            "channel_outbox": {
                "delivery_state",
                "source_event_id",
                "payload_ciphertext",
                "max_attempts",
                "next_attempt_at",
                "lease_owner",
                "lease_until",
                "dispatch_started_at",
                "dead_letter_at",
                "reconciled_at",
                "record_version",
            },
            "release_policies": {
                "tenant_id",
                "release_key",
                "version",
                "mode",
                "traffic_percentage",
                "status",
                "evaluation_passed",
                "latest_evaluation_run_id",
                "record_version",
            },
            "release_replay_runs": {
                "tenant_id",
                "release_id",
                "dataset_hash",
                "status",
                "severe_errors",
            },
            "release_observations": {
                "tenant_id",
                "release_id",
                "assignment_bucket",
                "action",
                "severe",
            },
            "evaluation_suites": {
                "tenant_id",
                "suite_key",
                "version",
                "previous_suite_id",
                "source_type",
                "required_scenarios_json",
                "thresholds_json",
                "status",
                "dataset_hash",
                "case_count",
                "latest_run_id",
                "record_version",
            },
            "evaluation_cases": {
                "tenant_id",
                "suite_id",
                "case_key",
                "scenario",
                "turns_json",
                "case_hash",
                "input_redacted",
            },
            "evaluation_runs": {
                "tenant_id",
                "suite_id",
                "release_id",
                "baseline_run_id",
                "run_key",
                "request_hash",
                "status",
                "runner_version",
                "dataset_hash",
                "metrics_json",
                "gate_json",
                "release_gate_applied",
                "release_gate_error",
            },
            "evaluation_case_results": {
                "tenant_id",
                "run_id",
                "case_id",
                "case_key",
                "scenario",
                "passed",
                "severe",
                "violations_json",
                "actual_json",
            },
            "handoff_queues": {
                "tenant_id",
                "queue_key",
                "status",
                "default_priority",
                "first_response_sla_minutes",
                "resolution_sla_minutes",
                "max_active_per_operator",
                "escalation_queue_id",
                "match_reasons_json",
                "match_intents_json",
                "match_risk_levels_json",
                "routing_order",
                "record_version",
            },
            "handoff_tasks": {
                "tenant_id",
                "queue_id",
                "priority",
                "sla_first_response_at",
                "sla_resolution_at",
                "acknowledged_at",
                "started_at",
                "review_started_at",
                "escalated_at",
                "escalation_level",
                "escalation_reason",
                "version",
            },
            "handoff_task_events": {
                "tenant_id",
                "handoff_id",
                "event_type",
                "from_status",
                "to_status",
                "from_queue_id",
                "to_queue_id",
                "from_assignee",
                "to_assignee",
                "task_version",
                "actor",
                "note_redacted",
            },
            "handoff_operator_profiles": {
                "tenant_id",
                "admin_id",
                "display_name",
                "status",
                "presence",
                "max_active_tasks",
                "skills_json",
                "record_version",
                "dispatch_mode",
                "schedule_mode",
                "presence_version",
                "presence_session_id",
                "presence_sequence",
                "presence_updated_at",
                "presence_expires_at",
            },
            "handoff_operator_queue_memberships": {
                "operator_profile_id",
                "tenant_id",
                "queue_id",
                "skill_level",
                "is_primary",
            },
            "handoff_operator_shifts": {
                "tenant_id",
                "operator_profile_id",
                "starts_at",
                "ends_at",
                "status",
                "record_version",
            },
            "handoff_dispatch_jobs": {
                "tenant_id",
                "handoff_id",
                "queue_id",
                "priority",
                "status",
                "attempt_count",
                "available_at",
                "lease_owner",
                "lease_expires_at",
                "assigned_to",
                "last_error",
                "record_version",
            },
            "handoff_dispatch_alerts": {
                "tenant_id",
                "handoff_id",
                "queue_id",
                "status",
                "reason",
                "occurrence_count",
                "detail_json",
                "record_version",
            },
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        issues: list[str] = []
        for table, expected_columns in required.items():
            if table not in tables:
                issues.append(f"missing table {table}")
                continue
            actual_columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            missing = sorted(expected_columns - actual_columns)
            if missing:
                issues.append(f"{table} missing columns {','.join(missing)}")
        if issues:
            raise RuntimeError("database schema validation failed: " + "; ".join(issues))

    def schema_version(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def audit(
        self,
        event_type: str,
        actor: str,
        subject_id: str | None,
        detail: dict[str, Any],
        tenant_id: str | None = None,
    ) -> str:
        event_id = f"audit-{uuid.uuid4().hex}"
        with self._write_lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log(
                    id, event_type, actor, subject_id, detail_json, created_at, tenant_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    actor,
                    subject_id,
                    json.dumps(detail, ensure_ascii=False),
                    utc_now(),
                    tenant_id,
                ),
            )
        return event_id

    def recent_messages(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, created_at FROM messages
                WHERE session_id = ? ORDER BY created_at DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def paginated_messages(
        self,
        session_id: str,
        cursor: str | None,
        limit: int,
        *,
        tenant_id: str,
        subject_hash: str,
    ) -> dict[str, Any]:
        decoded_cursor: tuple[str, str] | None = None
        if cursor:
            try:
                created_at, message_id = cursor.rsplit("|", 1)
                datetime.fromisoformat(created_at)
                if created_at and message_id:
                    decoded_cursor = (created_at, message_id)
            except (TypeError, ValueError):
                decoded_cursor = None

        page_limit = max(1, min(100, limit))
        conditions = [
            "m.session_id=?",
            "s.tenant_id=?",
            "s.subject_hash=?",
        ]
        params: list[Any] = [session_id, tenant_id, subject_hash]
        if decoded_cursor is not None:
            conditions.append(
                "(m.created_at > ? OR (m.created_at = ? AND m.id > ?))"
            )
            params.extend(
                [
                    decoded_cursor[0],
                    decoded_cursor[0],
                    decoded_cursor[1],
                ]
            )
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.id, m.trace_id, m.role, m.content, m.intent,
                       m.risk_level, m.route_reason, m.sources_json,
                       m.model_fallback, m.redacted, m.context_snapshot_id,
                       m.created_at
                FROM messages m
                JOIN sessions s ON s.id=m.session_id
                WHERE {' AND '.join(conditions)}
                ORDER BY m.created_at, m.id
                LIMIT ?
                """,
                (*params, page_limit + 1),
            ).fetchall()

        has_more = len(rows) > page_limit
        page_rows = rows[:page_limit]
        items = [dict(row) for row in page_rows]
        next_cursor = None
        if has_more and page_rows:
            next_cursor = f"{page_rows[-1]['created_at']}|{page_rows[-1]['id']}"
        return {
            "items": items,
            "next_cursor": next_cursor,
            "limit": page_limit,
        }

    def get_message_pair(
        self, assistant_message_id: str, tenant_id: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        with self.connect() as conn:
            query = "SELECT * FROM messages WHERE id = ? AND role = 'assistant'"
            params: tuple[Any, ...] = (assistant_message_id,)
            if tenant_id is not None:
                query += " AND tenant_id = ?"
                params = (assistant_message_id, tenant_id)
            assistant = conn.execute(query, params).fetchone()
            if assistant is None:
                return None
            user = conn.execute(
                """
                SELECT * FROM messages
                WHERE session_id = ? AND role = 'user' AND created_at <= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (assistant["session_id"], assistant["created_at"]),
            ).fetchone()
        if user is None:
            return None
        return dict(user), dict(assistant)

    def resolve_session(
        self,
        *,
        tenant_id: str,
        client_id: str,
        external_session_id: str,
        subject_hash: str,
        source_type: str = "api",
        source_reference: str | None = None,
    ) -> str:
        if source_type not in SESSION_SOURCE_TYPES:
            raise SessionScopeError("invalid session source type")
        if source_reference is not None and len(source_reference) > 128:
            raise SessionScopeError("session source reference is too long")
        with self._write_lock, self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE tenant_id=? AND external_session_id=?",
                (tenant_id, external_session_id),
            ).fetchone()
            if row is not None:
                if row["subject_hash"] != subject_hash or row["client_id"] != client_id:
                    raise SessionScopeError("session id is already bound to another authenticated scope")
                if row["source_type"] != source_type:
                    raise SessionScopeError("session id is already bound to another source type")
                if row["source_reference"] != source_reference:
                    raise SessionScopeError("session id is already bound to another source reference")
                if row["status"] != "active":
                    raise SessionScopeError("session is closed")
                conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (utc_now(), row["id"]))
                return str(row["id"])

            session_id = f"session-{uuid.uuid4().hex}"
            now = utc_now()
            conn.execute(
                """
                INSERT INTO sessions(
                    id, tenant_id, external_session_id, subject_hash, client_id,
                    status, created_at, last_seen_at, source_type, source_reference
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    session_id,
                    tenant_id,
                    external_session_id,
                    subject_hash,
                    client_id,
                    now,
                    now,
                    source_type,
                    source_reference,
                ),
            )
            return session_id

    def external_session_id(self, internal_session_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT external_session_id FROM sessions WHERE id=?", (internal_session_id,)
            ).fetchone()
        return str(row[0]) if row else "unknown"

    def record_metric(
        self,
        *,
        trace_id: str,
        tenant_id: str,
        session_id: str,
        intent: str,
        route_reason: str,
        success: bool,
        model_fallback: bool,
        requires_human: bool,
        duration_ms: float,
    ) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO request_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trace_id,
                    tenant_id,
                    session_id,
                    intent,
                    route_reason,
                    int(success),
                    int(model_fallback),
                    int(requires_human),
                    duration_ms,
                    utc_now(),
                ),
            )

    def metric_summary(
        self, tenant_id: str | None = None, *, scope: str = "all"
    ) -> dict[str, Any]:
        if scope not in SESSION_SCOPES:
            raise ValueError("invalid session scope")
        if scope == "operational":
            scope_condition = "(s.id IS NULL OR s.source_type NOT IN ('simulation','evaluation'))"
        elif scope == "simulation":
            scope_condition = "s.source_type='simulation'"
        elif scope == "evaluation":
            scope_condition = "s.source_type='evaluation'"
        else:
            scope_condition = "1=1"
        conditions = [scope_condition]
        params: list[Any] = []
        if tenant_id:
            conditions.append("r.tenant_id=?")
            params.append(tenant_id)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT r.duration_ms, r.success, r.model_fallback, r.requires_human
                FROM request_metrics r LEFT JOIN sessions s ON s.id=r.session_id
                WHERE {' AND '.join(conditions)}
                """,
                tuple(params),
            ).fetchall()
        durations = sorted(float(row["duration_ms"]) for row in rows)
        total = len(rows)
        p95_index = max(0, min(total - 1, math.ceil(total * 0.95) - 1)) if total else 0
        return {
            "requests": total,
            "success_rate": round(sum(row["success"] for row in rows) / total, 4) if total else None,
            "model_fallback_rate": round(sum(row["model_fallback"] for row in rows) / total, 4)
            if total
            else None,
            "human_handoff_rate": round(sum(row["requires_human"] for row in rows) / total, 4)
            if total
            else None,
            "latency_ms_avg": round(sum(durations) / total, 2) if total else None,
            "latency_ms_p95": round(durations[p95_index], 2) if total else None,
        }
