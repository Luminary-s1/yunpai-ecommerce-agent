import sqlite3

import pytest

from ecommerce_agent.database import Database
from ecommerce_agent.releases import ReleasePolicyCreateRequest, ReleaseService


def test_legacy_v1_database_upgrades_without_rebuild(tmp_path) -> None:
    db = Database(tmp_path / "legacy.sqlite3")
    db.path.parent.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        Database._apply_v1(conn)
    db.initialize()

    assert db.schema_version() == Database.SCHEMA_VERSION
    with db.connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert {
        "api_clients",
        "sessions",
        "handoff_tasks",
        "handoff_queues",
        "handoff_task_events",
        "handoff_operator_profiles",
        "handoff_operator_queue_memberships",
        "request_metrics",
        "platform_connections",
        "platform_oauth_states",
        "channel_conversations",
        "channel_events",
        "channel_outbox",
        "inventory_balances",
        "competitor_observations",
        "competitive_monitors",
        "competitive_alerts",
        "context_snapshots",
        "connector_sync_runs",
        "catalog_items",
        "commerce_orders",
        "commerce_order_lines",
        "commerce_order_logistics",
        "commerce_after_sale_cases",
        "commerce_order_events",
        "sop_definitions",
        "sop_versions",
        "sop_runs",
        "sop_step_runs",
        "qa_results",
        "channel_reply_drafts",
        "release_policies",
        "release_replay_runs",
        "release_observations",
        "agent_invocations",
        "channel_agent_jobs",
    } <= tables
    assert {"tenant_id", "client_id", "redacted", "context_snapshot_id"} <= message_columns
    assert {"source_type", "source_reference"} <= session_columns
    with db.connect() as conn:
        handoff_columns = {row[1] for row in conn.execute("PRAGMA table_info(handoff_tasks)")}
        client_columns = {row[1] for row in conn.execute("PRAGMA table_info(api_clients)")}
        knowledge_columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge)")}
        feedback_columns = {row[1] for row in conn.execute("PRAGMA table_info(feedback)")}
        competitor_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(competitor_observations)")
        }
        outbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(channel_outbox)")}
    assert {
        "acceptance_criteria",
        "deadline_at",
        "max_retries",
        "retry_count",
        "queue_id",
        "priority",
        "sla_first_response_at",
        "sla_resolution_at",
        "acknowledged_at",
        "escalation_level",
    } <= handoff_columns
    assert "role" in client_columns
    assert "tenant_id" in knowledge_columns
    assert "evidence_source" in feedback_columns
    assert "payload_hash" in competitor_columns
    assert {
        "delivery_state",
        "source_event_id",
        "payload_ciphertext",
        "actor",
        "allow_bot",
        "max_attempts",
        "next_attempt_at",
        "lease_owner",
        "lease_until",
        "dispatch_started_at",
        "dead_letter_at",
        "reconciled_at",
        "record_version",
    } <= outbox_columns
    assert {"knowledge_key", "layer", "review_status", "record_version"} <= knowledge_columns


def test_v7_database_upgrades_to_v8_without_losing_competitor_data(tmp_path) -> None:
    db = Database(tmp_path / "v7.sqlite3")
    db.path.parent.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 8):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-21T00:00:00+00:00"),
            )
        conn.execute(
            """
            INSERT INTO competitor_observations(
                id, tenant_id, connector_id, store_id, subject_sku,
                competitor_name, competitor_sku, subject_price, competitor_price,
                currency, source_type, source_ref, is_estimate, observed_at,
                source_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-observation", "tenant-a", "legacy", "store-a", "sku-a",
                "历史竞品", "comp-a", "100", "90", "CNY", "manual",
                "file://legacy.csv", 0, "2026-07-20T00:00:00+00:00",
                "legacy-source", "2026-07-20T00:00:00+00:00",
            ),
        )

    db.initialize()

    assert db.schema_version() == Database.SCHEMA_VERSION
    with db.connect() as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(competitor_observations)")
        }
        row = conn.execute(
            "SELECT id, payload_hash FROM competitor_observations WHERE id='legacy-observation'"
        ).fetchone()
    assert "payload_hash" in columns
    assert row["id"] == "legacy-observation"
    assert row["payload_hash"] == ""


def test_schema_markers_cannot_hide_missing_physical_tables(tmp_path) -> None:
    db = Database(tmp_path / "drifted.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            [(version, "2026-07-21T00:00:00+00:00") for version in range(1, 13)],
        )

    with pytest.raises(RuntimeError, match="database schema validation failed"):
        db.initialize()


def test_v9_inflight_outbox_is_not_silently_resent_during_upgrade(tmp_path) -> None:
    db = Database(tmp_path / "v9-outbox.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 10):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-21T00:00:00+00:00"),
            )
        conn.execute(
            """
            INSERT INTO channel_conversations(
                id, tenant_id, platform, shop_id, external_conversation_id,
                buyer_hash, buyer_nick_masked, owner_mode, assigned_to, version,
                last_event_id, last_message_at, created_at, updated_at
            ) VALUES ('conversation-v9', 'tenant-a', 'taobao', 'shop-a', 'external-v9',
                      'buyer-hash', '买***家', 'human', 'admin-a', 1,
                      'event-v9', '2026-07-21T00:00:00+00:00',
                      '2026-07-21T00:00:00+00:00', '2026-07-21T00:00:00+00:00')
            """
        )
        for item_id, status, attempt_count in (
            ("outbox-inflight", "sending", 1),
            ("outbox-queued", "queued", 0),
        ):
            conn.execute(
                """
                INSERT INTO channel_outbox(
                    id, tenant_id, conversation_id, event_id, idempotency_key,
                    content_redacted, status, attempt_count, platform_result_json,
                    last_error, created_at, updated_at, delivery_state
                ) VALUES (?, 'tenant-a', 'conversation-v9', NULL, ?, '历史回复', ?, ?,
                          NULL, NULL, '2026-07-21T00:00:00+00:00',
                          '2026-07-21T00:00:00+00:00', 'pending')
                """,
                (item_id, f"reply:{item_id}", status, attempt_count),
            )

    db.initialize()

    with db.connect() as conn:
        rows = {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT id, status, delivery_state, error_kind FROM channel_outbox"
            ).fetchall()
        }
    assert rows["outbox-inflight"] == {
        "id": "outbox-inflight",
        "status": "failed",
        "delivery_state": "uncertain",
        "error_kind": "legacy_inflight",
    }
    assert rows["outbox-queued"] == {
        "id": "outbox-queued",
        "status": "failed",
        "delivery_state": "dead_letter",
        "error_kind": "legacy_payload_missing",
    }


def test_v12_active_sop_run_gains_resumable_step_ledger(tmp_path) -> None:
    db = Database(tmp_path / "v12-sop.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 13):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-21T00:00:00+00:00"),
            )
        now = "2026-07-21T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO api_clients(
                id, tenant_id, name, key_salt, key_hash, key_iterations,
                can_supply_order_context, status, created_at, updated_at
            ) VALUES ('client-v12', 'tenant-v12', 'legacy', X'00', X'00', 1, 0,
                      'active', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(
                id, tenant_id, external_session_id, subject_hash, client_id,
                status, created_at, last_seen_at
            ) VALUES ('session-v12', 'tenant-v12', 'external-v12', 'subject-v12',
                      'client-v12', 'active', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sop_definitions(
                id, tenant_id, sop_key, name, intent, risk_level,
                current_active_version, record_version, created_at, updated_at
            ) VALUES ('definition-v12', 'tenant-v12', 'legacy.order', '历史订单 SOP',
                      'order', 'high', 1, 1, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sop_versions(
                id, definition_id, version, dsl_json, checksum, status,
                evaluation_json, created_by, approved_by, created_at,
                evaluated_at, approved_at, activated_at, retired_at
            ) VALUES ('version-v12', 'definition-v12', 1, ?, 'legacy', 'active',
                      '{}', 'legacy', 'legacy', ?, ?, ?, ?, NULL)
            """,
            (
                '{"trigger":{"intents":["order"]},"required_context":[],"steps":'
                '[{"observe":"get_order_facts"},{"evaluate":"order_policy"}],'
                '"guards":{"allow_external_write":false},'
                '"handoff":{"when":["conflict"]},'
                '"success":{"postcondition":"order_checked"}}',
                now,
                now,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO sop_runs(
                id, tenant_id, session_id, definition_id, sop_version_id,
                status, outcome_json, started_at, completed_at
            ) VALUES ('run-v12', 'tenant-v12', 'session-v12', 'definition-v12',
                      'version-v12', 'active', '{}', ?, NULL)
            """,
            (now,),
        )

    db.initialize()

    with db.connect() as conn:
        run = conn.execute(
            "SELECT current_step_index, record_version, updated_at FROM sop_runs WHERE id='run-v12'"
        ).fetchone()
        steps = conn.execute(
            """
            SELECT step_id, step_index, operation, capability, status
            FROM sop_step_runs WHERE run_id='run-v12' ORDER BY step_index
            """
        ).fetchall()
    assert db.schema_version() == Database.SCHEMA_VERSION
    assert dict(run) == {
        "current_step_index": 0,
        "record_version": 1,
        "updated_at": now,
    }
    assert [dict(step) for step in steps] == [
        {
            "step_id": "step_01",
            "step_index": 0,
            "operation": "observe",
            "capability": "get_order_facts",
            "status": "pending",
        },
        {
            "step_id": "step_02",
            "step_index": 1,
            "operation": "evaluate",
            "capability": "order_policy",
            "status": "pending",
        },
    ]


def test_v13_database_gains_competitive_monitoring_tables(tmp_path) -> None:
    db = Database(tmp_path / "v13-competitive.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 14):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-21T00:00:00+00:00"),
            )

    db.initialize()

    assert db.schema_version() == Database.SCHEMA_VERSION
    with db.connect() as conn:
        monitor_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(competitive_monitors)")
        }
        alert_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(competitive_alerts)")
        }
        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(competitive_alerts)"
        ).fetchall()
    assert {
        "tenant_id",
        "store_id",
        "subject_sku",
        "undercut_threshold_percent",
        "record_version",
    } <= monitor_columns
    assert {
        "monitor_id",
        "alert_code",
        "status",
        "evidence_key",
        "occurrence_count",
    } <= alert_columns
    assert any(row[2] == "competitive_monitors" for row in foreign_keys)


def test_v15_database_gains_durable_channel_agent_tables_without_replaying_history(
    tmp_path,
) -> None:
    db = Database(tmp_path / "v15-channel-agent.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 16):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-22T00:00:00+00:00"),
            )
        now = "2026-07-22T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO channel_conversations(
                id, tenant_id, platform, shop_id, external_conversation_id,
                buyer_hash, buyer_nick_masked, owner_mode, assigned_to, version,
                last_event_id, last_message_at, created_at, updated_at
            ) VALUES ('conversation-v15', 'tenant-v15', 'taobao', 'shop-v15',
                      'external-v15', 'buyer-v15', '买***家', 'bot', NULL, 1,
                      'external-event-v15', ?, ?, ?)
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO channel_events(
                id, tenant_id, platform, shop_id, conversation_id, external_event_id,
                direction, message_type, content_redacted, payload_hash,
                routing_ciphertext, request_id, action_mode, status, created_at, updated_at
            ) VALUES ('event-v15', 'tenant-v15', 'taobao', 'shop-v15',
                      'conversation-v15', 'external-event-v15', 'inbound', '1',
                      '历史消息', 'hash-v15', NULL, NULL, NULL, 'received', ?, ?)
            """,
            (now, now),
        )

    db.initialize()

    assert db.schema_version() == Database.SCHEMA_VERSION
    with db.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        historic_jobs = conn.execute(
            "SELECT COUNT(*) FROM channel_agent_jobs"
        ).fetchone()[0]
    assert {"agent_invocations", "channel_agent_jobs"} <= tables
    assert historic_jobs == 0


def test_v16_database_gains_competitive_entity_evidence_without_reclassifying_history(
    tmp_path,
) -> None:
    db = Database(tmp_path / "v16-competitive-entity.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 17):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-22T00:00:00+00:00"),
            )
        now = "2026-07-22T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO competitor_observations(
                id, tenant_id, connector_id, store_id, subject_sku,
                competitor_name, competitor_sku, subject_price, competitor_price,
                currency, source_type, source_ref, is_estimate, observed_at,
                source_id, created_at, payload_hash
            ) VALUES ('observation-v16', 'tenant-v16', 'legacy-feed', 'store-v16',
                      'sku-v16', '历史竞店', 'comp-v16', '100', '90', 'CNY',
                      'manual', 'file://legacy-price.csv', 0, ?, 'source-v16', ?, 'hash-v16')
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO competitive_monitors(
                id, tenant_id, store_id, subject_sku, enabled,
                undercut_threshold_percent, price_drop_threshold_percent,
                stale_after_hours, include_estimates, created_by,
                record_version, created_at, updated_at
            ) VALUES ('monitor-v16', 'tenant-v16', 'store-v16', 'sku-v16', 1,
                      '5.00', '5.00', 24, 0, 'legacy-admin', 1, ?, ?)
            """,
            (now, now),
        )

    db.initialize()

    assert db.schema_version() == Database.SCHEMA_VERSION
    with db.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        observation = conn.execute(
            "SELECT entity_match_id FROM competitor_observations WHERE id='observation-v16'"
        ).fetchone()
        monitor = conn.execute(
            "SELECT require_approved_match FROM competitive_monitors WHERE id='monitor-v16'"
        ).fetchone()
    assert {
        "competitive_entity_matches",
        "competitive_match_decisions",
        "competitive_signals",
    } <= tables
    assert observation["entity_match_id"] is None
    assert monitor["require_approved_match"] == 0


def test_v17_database_upgrades_to_current_without_losing_release_policy(tmp_path) -> None:
    db = Database(tmp_path / "v17-evaluations.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 18):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-22T00:00:00+00:00"),
            )

    release = ReleaseService(db).create(
        "tenant-v17",
        ReleasePolicyCreateRequest(
            release_key="customer-service",
            name="历史客服发布策略",
            platform="taobao",
            store_id="store-v17",
            mode="shadow",
            intent_allowlist=["faq"],
        ),
        "legacy-admin",
    )

    db.initialize()

    assert db.schema_version() == Database.SCHEMA_VERSION
    with db.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        preserved = conn.execute(
            """
            SELECT id, tenant_id, release_key, status, latest_evaluation_run_id
            FROM release_policies WHERE id=?
            """,
            (release["id"],),
        ).fetchone()
        evaluation_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "evaluation_suites",
                "evaluation_cases",
                "evaluation_runs",
                "evaluation_case_results",
            )
        }
    assert {
        "evaluation_suites",
        "evaluation_cases",
        "evaluation_runs",
        "evaluation_case_results",
    } <= tables
    assert dict(preserved) == {
        "id": release["id"],
        "tenant_id": "tenant-v17",
        "release_key": "customer-service",
        "status": "draft",
        "latest_evaluation_run_id": None,
    }
    assert evaluation_counts == {
        "evaluation_suites": 0,
        "evaluation_cases": 0,
        "evaluation_runs": 0,
        "evaluation_case_results": 0,
    }


def test_v18_handoff_tasks_gain_queue_sla_and_event_history_without_state_loss(
    tmp_path,
) -> None:
    db = Database(tmp_path / "v18-handoff-workbench.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 19):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-22T00:00:00+00:00"),
            )
        now = "2026-07-22T00:00:00+00:00"
        deadline = "2026-07-23T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO api_clients(
                id, tenant_id, name, key_salt, key_hash, key_iterations,
                can_supply_order_context, status, created_at, updated_at
            ) VALUES ('client-v18', 'tenant-v18', 'legacy', X'00', X'00', 1, 0,
                      'active', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(
                id, tenant_id, external_session_id, subject_hash, client_id,
                status, created_at, last_seen_at
            ) VALUES ('session-v18', 'tenant-v18', 'external-v18', 'subject-v18',
                      'client-v18', 'active', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO handoff_tasks(
                id, tenant_id, session_id, message_id, status, reason, payload_json,
                acceptance_criteria, assigned_to, deadline_at, max_retries,
                retry_count, version, created_at, updated_at, completed_at
            ) VALUES ('handoff-v18', 'tenant-v18', 'session-v18', 'message-v18',
                      'working', 'legacy_reason', '{}', 'legacy acceptance',
                      'operator-v18', ?, 2, 1, 4, ?, ?, NULL)
            """,
            (deadline, now, now),
        )

    db.initialize()

    with db.connect() as conn:
        task = conn.execute(
            """
            SELECT status, assigned_to, priority, sla_first_response_at,
                   sla_resolution_at, queue_id, version
            FROM handoff_tasks WHERE id='handoff-v18'
            """
        ).fetchone()
        queue = conn.execute(
            "SELECT queue_key FROM handoff_queues WHERE id=?", (task["queue_id"],)
        ).fetchone()
        event = conn.execute(
            """
            SELECT event_type, to_status, to_assignee, task_version
            FROM handoff_task_events WHERE handoff_id='handoff-v18'
            """
        ).fetchone()
    assert db.schema_version() == Database.SCHEMA_VERSION
    assert task["status"] == "working"
    assert task["assigned_to"] == "operator-v18"
    assert task["priority"] == "normal"
    assert task["sla_first_response_at"] == deadline
    assert task["sla_resolution_at"] == deadline
    assert task["version"] == 4
    assert queue["queue_key"] == "general"
    assert dict(event) == {
        "event_type": "migrated",
        "to_status": "working",
        "to_assignee": "operator-v18",
        "task_version": 4,
    }


def test_v19_admins_gain_staffing_profiles_and_tenant_safe_memberships(
    tmp_path,
) -> None:
    db = Database(tmp_path / "v19-staffing.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 20):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-22T00:00:00+00:00"),
            )
        now = "2026-07-22T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO api_clients(
                id, tenant_id, name, key_salt, key_hash, key_iterations,
                can_supply_order_context, status, created_at, updated_at, role
            ) VALUES ('admin-v19', 'tenant-v19', 'Legacy operator', X'00', X'00', 1,
                      0, 'active', ?, ?, 'admin')
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO handoff_queues(
                id, tenant_id, queue_key, name, description, status,
                default_priority, first_response_sla_minutes,
                resolution_sla_minutes, max_active_per_operator,
                escalation_queue_id, match_reasons_json, match_intents_json,
                match_risk_levels_json, routing_order, record_version,
                created_by, created_at, updated_at
            ) VALUES ('queue-v19', 'tenant-v19', 'general', 'Legacy queue', '', 'active',
                      'normal', 30, 480, 20, NULL, '[]', '[]', '[]', 999, 1,
                      'legacy', ?, ?)
            """,
            (now, now),
        )

    db.initialize()

    with db.connect() as conn:
        profile = conn.execute(
            """
            SELECT admin_id, display_name, status, presence, presence_expires_at,
                   record_version
            FROM handoff_operator_profiles WHERE admin_id='admin-v19'
            """
        ).fetchone()
        membership = conn.execute(
            """
            SELECT m.tenant_id, q.queue_key, m.skill_level, m.is_primary
            FROM handoff_operator_queue_memberships m
            JOIN handoff_queues q ON q.id=m.queue_id
            WHERE q.id='queue-v19'
            """
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="tenant mismatch"):
            conn.execute(
                """
                UPDATE handoff_operator_queue_memberships SET tenant_id='tenant-other'
                WHERE queue_id='queue-v19'
                """
            )
    assert db.schema_version() == Database.SCHEMA_VERSION
    assert dict(profile) == {
        "admin_id": "admin-v19",
        "display_name": "Legacy operator",
        "status": "active",
        "presence": "available",
        "presence_expires_at": profile["presence_expires_at"],
        "record_version": 1,
    }
    assert profile["presence_expires_at"]
    assert dict(membership) == {
        "tenant_id": "tenant-v19",
        "queue_key": "general",
        "skill_level": 3,
        "is_primary": 1,
    }


def test_v20_database_gains_schedules_heartbeats_and_durable_dispatch(tmp_path) -> None:
    db = Database(tmp_path / "v20-dispatch.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 21):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-22T00:00:00+00:00"),
            )
        now = "2026-07-22T01:00:00+00:00"
        conn.execute(
            """
            INSERT INTO api_clients(
                id, tenant_id, name, key_salt, key_hash, key_iterations,
                can_supply_order_context, status, created_at, updated_at, role
            ) VALUES ('admin-v20', 'tenant-v20', 'Legacy operator', X'00', X'00', 1,
                      0, 'active', ?, ?, 'admin')
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(
                id, tenant_id, external_session_id, subject_hash, client_id,
                status, created_at, last_seen_at
            ) VALUES ('session-v20', 'tenant-v20', 'external-v20', 'subject-v20',
                      'admin-v20', 'active', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO handoff_queues(
                id, tenant_id, queue_key, name, description, status,
                default_priority, first_response_sla_minutes,
                resolution_sla_minutes, max_active_per_operator,
                escalation_queue_id, match_reasons_json, match_intents_json,
                match_risk_levels_json, routing_order, record_version,
                created_by, created_at, updated_at
            ) VALUES ('queue-v20', 'tenant-v20', 'general', 'Legacy queue', '', 'active',
                      'high', 15, 240, 10, NULL, '[]', '[]', '[]', 100, 1,
                      'legacy', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO handoff_operator_profiles(
                id, tenant_id, admin_id, display_name, status, presence,
                max_active_tasks, skills_json, record_version,
                presence_updated_at, presence_expires_at, created_by,
                created_at, updated_at
            ) VALUES ('operator-v20', 'tenant-v20', 'admin-v20', 'Legacy operator',
                      'active', 'available', 10, '["refund"]', 3, ?, ?,
                      'legacy', ?, ?)
            """,
            (now, "2026-07-23T01:00:00+00:00", now, now),
        )
        conn.execute(
            """
            INSERT INTO handoff_operator_queue_memberships(
                operator_profile_id, tenant_id, queue_id, skill_level,
                is_primary, created_at, updated_at
            ) VALUES ('operator-v20', 'tenant-v20', 'queue-v20', 4, 1, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO handoff_tasks(
                id, tenant_id, session_id, message_id, status, reason,
                payload_json, acceptance_criteria, assigned_to, deadline_at,
                max_retries, retry_count, version, created_at, updated_at,
                completed_at, queue_id, priority, sla_first_response_at,
                sla_resolution_at, acknowledged_at, escalation_level
            ) VALUES ('handoff-v20', 'tenant-v20', 'session-v20', 'message-v20',
                      'proposed', 'legacy_reason', '{}', 'legacy acceptance', NULL,
                      '2026-07-22T05:00:00+00:00', 2, 0, 2, ?, ?, NULL,
                      'queue-v20', 'high', '2026-07-22T01:15:00+00:00',
                      '2026-07-22T05:00:00+00:00', NULL, 0)
            """,
            (now, now),
        )

    db.initialize()

    with db.connect() as conn:
        profile = conn.execute(
            """
            SELECT dispatch_mode, schedule_mode, presence_version,
                   presence_session_id, presence_sequence, record_version
            FROM handoff_operator_profiles WHERE id='operator-v20'
            """
        ).fetchone()
        membership = conn.execute(
            """
            SELECT skill_level, is_primary FROM handoff_operator_queue_memberships
            WHERE operator_profile_id='operator-v20' AND queue_id='queue-v20'
            """
        ).fetchone()
        job = conn.execute(
            """
            SELECT handoff_id, queue_id, priority, status, attempt_count
            FROM handoff_dispatch_jobs WHERE handoff_id='handoff-v20'
            """
        ).fetchone()
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        migration_version = conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]

    assert db.schema_version() == Database.SCHEMA_VERSION
    assert dict(profile) == {
        "dispatch_mode": "automatic",
        "schedule_mode": "unrestricted",
        "presence_version": 1,
        "presence_session_id": None,
        "presence_sequence": 0,
        "record_version": 3,
    }
    assert dict(membership) == {"skill_level": 4, "is_primary": 1}
    assert dict(job) == {
        "handoff_id": "handoff-v20",
        "queue_id": "queue-v20",
        "priority": "high",
        "status": "pending",
        "attempt_count": 0,
    }
    assert {"handoff_operator_shifts", "handoff_dispatch_alerts"} <= tables
    assert migration_version == Database.SCHEMA_VERSION


def test_v21_session_sources_are_classified_during_upgrade(tmp_path) -> None:
    db = Database(tmp_path / "v21-session-sources.sqlite3")
    db.path.parent.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 22):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-22T00:00:00+00:00"),
            )
        conn.execute(
            """
            INSERT INTO api_clients(
                id, tenant_id, name, key_salt, key_hash, key_iterations,
                can_supply_order_context, status, created_at, updated_at
            ) VALUES ('client-v21', 'tenant-v21', 'legacy client', X'00', X'00',
                      1, 0, 'active', ?, ?)
            """,
            ("2026-07-22T00:00:00+00:00", "2026-07-22T00:00:00+00:00"),
        )
        for session_id, external_id in (
            ("session-api", "shop-live-1"),
            ("session-channel", "taobao:shop-a:buyer-a"),
            ("session-simulation", "virtual-customer-service-run-a"),
            ("session-evaluation", "evaluation:release-a:case-a"),
        ):
            conn.execute(
                """
                INSERT INTO sessions(
                    id, tenant_id, external_session_id, subject_hash, client_id,
                    status, created_at, last_seen_at
                ) VALUES (?, 'tenant-v21', ?, 'subject-v21', 'client-v21',
                          'active', ?, ?)
                """,
                (
                    session_id,
                    external_id,
                    "2026-07-22T00:00:00+00:00",
                    "2026-07-22T00:00:00+00:00",
                ),
            )

    db.initialize()

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, source_type, source_reference
            FROM sessions ORDER BY id
            """
        ).fetchall()

    assert db.schema_version() == Database.SCHEMA_VERSION
    assert [dict(row) for row in rows] == [
        {"id": "session-api", "source_type": "api", "source_reference": None},
        {"id": "session-channel", "source_type": "channel", "source_reference": None},
        {"id": "session-evaluation", "source_type": "evaluation", "source_reference": None},
        {
            "id": "session-simulation",
            "source_type": "simulation",
            "source_reference": "legacy-virtual",
        },
    ]


def test_v23_database_gains_staged_rollouts_for_gray_release(tmp_path) -> None:
    db = Database(tmp_path / "v23-rollouts.sqlite3")
    db.path.parent.mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 24):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-26T00:00:00+00:00"),
            )

    db.initialize()

    assert db.schema_version() == Database.SCHEMA_VERSION
    with db.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(staged_rollouts)")}
        conn.execute(
            """
            INSERT INTO staged_rollouts(
                id, tenant_id, subject_type, subject_key, candidate_id, baseline_id,
                traffic_percentage, rollout_salt, status, note, record_version,
                created_by, completed_by, created_at, updated_at, completed_at
            ) VALUES ('rollout-a', 'tenant-a', 'knowledge', 'key-a', 'candidate-a',
                      NULL, 30, 'salt-a', 'active', NULL, 1, 'admin-a', NULL,
                      '2026-07-27T00:00:00+00:00', '2026-07-27T00:00:00+00:00', NULL)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO staged_rollouts(
                    id, tenant_id, subject_type, subject_key, candidate_id, baseline_id,
                    traffic_percentage, rollout_salt, status, note, record_version,
                    created_by, completed_by, created_at, updated_at, completed_at
                ) VALUES ('rollout-b', 'tenant-a', 'knowledge', 'key-a', 'candidate-b',
                          NULL, 60, 'salt-b', 'active', NULL, 1, 'admin-a', NULL,
                          '2026-07-27T00:00:00+00:00', '2026-07-27T00:00:00+00:00', NULL)
                """
            )
    assert {
        "tenant_id", "subject_type", "subject_key", "candidate_id",
        "traffic_percentage", "rollout_salt", "status", "record_version",
    } <= columns
