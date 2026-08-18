"""M9-R WP3 生命周期建议持久化：_apply_v36 迁移正确性测试（T1-T8）。

覆盖：
- T1 建表幂等 + 列存在（36 in migrations）
- T2 enum CHECK 与 Python 枚举一致性（type/state/from_state/to_state/action）
- T3 audit 不可变（UPDATE/DELETE 拒绝）
- T4 recommendations 状态可迁移（防误建成 readonly）
- T5 audit 外键拒绝孤儿 / 租户不符
- T6 v34 升级保留存量数据
- T8 _validate_schema 兜底（反证：required 漏登记即挂）

纪律（CONTRIBUTING）：
- 不断言全局 schema_version()==36，不断言 35 not in
- 只断言 36 in migrations + 自己的列/表/索引
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from ecommerce_agent.database import Database
from ecommerce_agent.product_lifecycle.schemas import (
    RecommendationState,
    RecommendationType,
)
from ecommerce_agent.product_lifecycle.state_machine import TransitionAction

_REC_COLUMNS = {
    "recommendation_id", "tenant_id", "recommendation_type",
    "store_id", "item_id", "sku_id",
    "facts_snapshot_json", "rationale", "missing_evidence_json",
    "alternatives_json", "state", "degraded",
    "payload_hash", "created_at", "updated_at",
}
_AUDIT_COLUMNS = {
    "audit_id", "recommendation_id", "tenant_id", "action",
    "from_state", "to_state", "actor", "occurred_at",
    "payload_hash",
}


def _insert_recommendation(
    conn: sqlite3.Connection,
    *,
    recommendation_id: str = "rec-1",
    tenant_id: str = "tenant-a",
    state: str = "draft",
) -> None:
    conn.execute(
        """
        INSERT INTO product_recommendations(
            recommendation_id, tenant_id, recommendation_type, store_id, item_id,
            sku_id, facts_snapshot_json, rationale, missing_evidence_json,
            alternatives_json, state, degraded, payload_hash, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            recommendation_id, tenant_id, "保持观察", "store-a", None, None,
            "{}", "observe", "[]", "[]",
            state, 0, "a" * 64, "2026-08-18T00:00:00+00:00", "2026-08-18T00:00:00+00:00",
        ),
    )


def _insert_audit(
    conn: sqlite3.Connection,
    *,
    recommendation_id: str = "rec-1",
    tenant_id: str = "tenant-a",
) -> None:
    conn.execute(
        """
        INSERT INTO product_recommendation_audit(
            tenant_id, recommendation_id, action, from_state, to_state,
            actor, occurred_at, payload_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tenant_id, recommendation_id, "submit", "draft", "awaiting_review",
         "ops-1", "2026-08-18T00:00:00+00:00", "b" * 64),
    )


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]


def _enum_check_values(table_sql: str, column: str) -> set[str]:
    match = re.search(
        rf"CHECK\s*\(\s*{re.escape(column)}\s+IN\s*\(([^)]*)\)\s*\)",
        table_sql,
        flags=re.IGNORECASE,
    )
    assert match is not None, f"missing enum CHECK for {column}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_v36_creates_lifecycle_persistence_tables_once(tmp_path) -> None:
    """T1：两次 initialize 幂等；36 登记；两表与列集存在。"""
    db = Database(tmp_path / "lifecycle-v36.sqlite3")
    db.initialize()
    db.initialize()

    with db.connect() as conn:
        migrations = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rec_columns = {row[1] for row in conn.execute("PRAGMA table_info(product_recommendations)")}
        audit_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(product_recommendation_audit)")
        }
        counts = dict(
            conn.execute(
                "SELECT version, COUNT(*) FROM schema_migrations WHERE version=36 GROUP BY version"
            ).fetchall()
        )

    assert 36 in migrations
    assert {"product_recommendations", "product_recommendation_audit"} <= tables
    assert _REC_COLUMNS <= rec_columns
    assert _AUDIT_COLUMNS <= audit_columns
    assert counts.get(36) == 1  # 幂等：迁移行只插一次


def test_v36_enum_checks_match_lifecycle_contract_enums(tmp_path) -> None:
    """T2：SQL CHECK 值集 == Python 枚举值集（防枚举漂移）。"""
    db = Database(tmp_path / "lifecycle-v36-enums.sqlite3")
    db.initialize()

    with db.connect() as conn:
        rec_sql = _table_sql(conn, "product_recommendations")
        audit_sql = _table_sql(conn, "product_recommendation_audit")

    assert _enum_check_values(rec_sql, "recommendation_type") == {
        member.value for member in RecommendationType
    }
    assert _enum_check_values(rec_sql, "state") == {member.value for member in RecommendationState}
    assert _enum_check_values(audit_sql, "action") == {member.value for member in TransitionAction}
    assert _enum_check_values(audit_sql, "from_state") == {
        member.value for member in RecommendationState
    }
    assert _enum_check_values(audit_sql, "to_state") == {
        member.value for member in RecommendationState
    }


def test_v36_audit_trail_is_immutable(tmp_path) -> None:
    """T3：审计记录不可变——UPDATE/DELETE 均被触发器拒绝。"""
    db = Database(tmp_path / "lifecycle-v36-immutable.sqlite3")
    db.initialize()

    with db.connect() as conn:
        _insert_recommendation(conn)
        _insert_audit(conn)

    with pytest.raises(sqlite3.IntegrityError, match="product_recommendation_audit_immutable"):
        with db.connect() as conn:
            conn.execute(
                "UPDATE product_recommendation_audit SET actor=? WHERE recommendation_id=?",
                ("changed", "rec-1"),
            )
    with pytest.raises(sqlite3.IntegrityError, match="product_recommendation_audit_immutable"):
        with db.connect() as conn:
            conn.execute(
                "DELETE FROM product_recommendation_audit WHERE recommendation_id=?",
                ("rec-1",),
            )


def test_v36_recommendation_state_transition_persists(tmp_path) -> None:
    """T4：建议可变——state 随状态机落库更新（正例，防误建成 readonly）。"""
    db = Database(tmp_path / "lifecycle-v36-state.sqlite3")
    db.initialize()

    with db.connect() as conn:
        _insert_recommendation(conn, state="draft")
        conn.execute(
            "UPDATE product_recommendations SET state=?, updated_at=? WHERE recommendation_id=?",
            ("awaiting_review", "2026-08-18T01:00:00+00:00", "rec-1"),
        )

    with db.connect() as conn:
        row = conn.execute(
            "SELECT state FROM product_recommendations WHERE recommendation_id='rec-1'"
        ).fetchone()

    assert row["state"] == "awaiting_review"


def test_v36_audit_fk_rejects_orphan_and_tenant_mismatch(tmp_path) -> None:
    """T5：审计外键拒绝孤儿记录与租户不符。"""
    db = Database(tmp_path / "lifecycle-v36-fk.sqlite3")
    db.initialize()

    with db.connect() as conn:
        _insert_recommendation(conn)
        # 孤儿：指向不存在的 recommendation_id（FK 立即校验，INSERT 即抛）
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            _insert_audit(conn, recommendation_id="does-not-exist")

    db = Database(tmp_path / "lifecycle-v36-fk2.sqlite3")
    db.initialize()
    with db.connect() as conn:
        _insert_recommendation(conn, tenant_id="tenant-a", recommendation_id="rec-1")
        # 租户不符：同 recommendation_id 但不同 tenant（复合外键立即拒绝）
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            _insert_audit(conn, tenant_id="tenant-b", recommendation_id="rec-1")


def test_v36_database_upgrades_from_v34_preserving_existing_data(tmp_path) -> None:
    """T6：从 v34 铺底升级到 v36，存量数据保留。"""
    db = Database(tmp_path / "lifecycle-v36-upgrade.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in [*range(1, 31), 32, 33, 34]:
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-08-17T00:00:00+00:00"),
            )
        conn.execute(
            """
            INSERT INTO creative_assets(
                asset_id, tenant_id, sha256, mime_type, width, height, storage_ref,
                source_ref, feature_schema_version, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset-v34", "tenant-a", "a" * 64, "image/png", 1, 1,
                "objects/assets/a.png", None, "traffic-creative-features-v1",
                "b" * 64, "2026-08-17T00:00:00+00:00", "2026-08-17T00:00:00+00:00",
            ),
        )
        conn.execute("PRAGMA user_version = 34")

    db.initialize()

    with db.connect() as conn:
        migrations = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        preserved = conn.execute(
            "SELECT asset_id FROM creative_assets WHERE asset_id='asset-v34'"
        ).fetchone()

    assert 36 in migrations
    assert {"product_recommendations", "product_recommendation_audit"} <= tables
    assert preserved["asset_id"] == "asset-v34"


def test_v36_validate_schema_detects_missing_recommendation_tables(tmp_path) -> None:
    """T8（反证）：删表后 initialize 必须报 schema validation failed。

    临时移除 _validate_schema.required 的 product_recommendations 条目后，
    本测试如期失败（required 不再报 missing table）——证明 required 登记被真正检验。
    """
    db = Database(tmp_path / "lifecycle-v36-schema-guard.sqlite3")
    db.initialize()
    with db.connect() as conn:
        conn.execute("DROP TABLE product_recommendations")

    with pytest.raises(RuntimeError, match="database schema validation failed"):
        db.initialize()
