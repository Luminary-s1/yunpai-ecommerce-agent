"""M9-R WP5 验收修复迁移 v37 测试。

覆盖：
- 复合主键 (tenant_id, recommendation_id)，跨租户同 ID 可共存（缺陷 6 反证）
- state CHECK 含 stale，与 Python 枚举对齐（缺陷 3 迁移侧）
- 内容列不可变触发器（仅 state/updated_at 可变）（缺陷 3 反证）
- v36 铺底升级到 v37 存量数据保留
- 幂等：重复 initialize 迁移行不重复
"""
from __future__ import annotations

import sqlite3

import pytest

from ecommerce_agent.database import Database
from ecommerce_agent.product_lifecycle.schemas import RecommendationState


def _seed_v36(db: Database) -> None:
    """铺 v36 旧库并插一条建议 + 审计。"""
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in [*range(1, 31), 32, 33, 34, 36]:
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-08-18T00:00:00+00:00"),
            )
        conn.execute(
            """
            INSERT INTO product_recommendations(
                recommendation_id, tenant_id, recommendation_type, store_id, item_id,
                sku_id, facts_snapshot_json, rationale, missing_evidence_json,
                alternatives_json, state, degraded, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rec-v36", "tenant-a", "保持观察", "store-a", None, None,
                "{}", "observe", "[]", "[]",
                "draft", 0, "a" * 64, "2026-08-18T00:00:00+00:00", "2026-08-18T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO product_recommendation_audit(
                tenant_id, recommendation_id, action, from_state, to_state,
                actor, occurred_at, payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("tenant-a", "rec-v36", "submit", "draft", "awaiting_review",
             "ops-1", "2026-08-18T00:00:00+00:00", "b" * 64),
        )
        conn.execute("PRAGMA user_version = 36")


def test_v37_composite_pk_allows_cross_tenant_same_id(tmp_path) -> None:
    """复合主键：跨租户同 recommendation_id 可共存（缺陷 6 反证）。"""
    db = Database(tmp_path / "v37-pk.sqlite3")
    db.initialize()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO product_recommendations(
                recommendation_id, tenant_id, recommendation_type, store_id, item_id,
                sku_id, facts_snapshot_json, rationale, missing_evidence_json,
                alternatives_json, state, degraded, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rec-1", "tenant-a", "保持观察", "store-a", None, None,
                "{}", "x", "[]", "[]", "draft", 0, "a" * 64,
                "2026-08-18T00:00:00+00:00", "2026-08-18T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO product_recommendations(
                recommendation_id, tenant_id, recommendation_type, store_id, item_id,
                sku_id, facts_snapshot_json, rationale, missing_evidence_json,
                alternatives_json, state, degraded, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rec-1", "tenant-b", "保持观察", "store-b", None, None,
                "{}", "x", "[]", "[]", "draft", 0, "b" * 64,
                "2026-08-18T00:00:00+00:00", "2026-08-18T00:00:00+00:00",
            ),
        )
        rows = conn.execute(
            "SELECT tenant_id, recommendation_id FROM product_recommendations WHERE recommendation_id='rec-1'"
        ).fetchall()
        assert {(r["tenant_id"], r["recommendation_id"]) for r in rows} == {
            ("tenant-a", "rec-1"), ("tenant-b", "rec-1"),
        }


def test_v37_state_check_includes_stale(tmp_path) -> None:
    """state CHECK 值集含 stale，与 Python 枚举对齐（缺陷 3 迁移侧）。"""
    import re

    db = Database(tmp_path / "v37-stale.sqlite3")
    db.initialize()
    with db.connect() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='product_recommendations'"
        ).fetchone()[0]
    match = re.search(r"CHECK\s*\(\s*state\s+IN\s*\(([^)]*)\)\s*\)", sql)
    assert match is not None
    values = set(re.findall(r"'([^']+)'", match.group(1)))
    assert values == {member.value for member in RecommendationState}


def test_v37_content_columns_immutable_state_mutable(tmp_path) -> None:
    """内容列 UPDATE 被拒，仅 state/updated_at 可变（缺陷 3 反证）。"""
    db = Database(tmp_path / "v37-immutable.sqlite3")
    db.initialize()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO product_recommendations(
                recommendation_id, tenant_id, recommendation_type, store_id, item_id,
                sku_id, facts_snapshot_json, rationale, missing_evidence_json,
                alternatives_json, state, degraded, payload_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rec-1", "tenant-a", "保持观察", "store-a", None, None,
                "{}", "x", "[]", "[]", "draft", 0, "a" * 64,
                "2026-08-18T00:00:00+00:00", "2026-08-18T00:00:00+00:00",
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="product_recommendations_content_immutable"):
        with db.connect() as conn:
            conn.execute(
                "UPDATE product_recommendations SET rationale='hacked' "
                "WHERE tenant_id='tenant-a' AND recommendation_id='rec-1'"
            )
    with pytest.raises(sqlite3.IntegrityError, match="product_recommendations_content_immutable"):
        with db.connect() as conn:
            conn.execute(
                "UPDATE product_recommendations SET payload_hash='c'*64 "
                "WHERE tenant_id='tenant-a' AND recommendation_id='rec-1'"
            )
    # state 可更新（状态机落库）
    with db.connect() as conn:
        conn.execute(
            "UPDATE product_recommendations SET state='awaiting_review', updated_at='2026-08-18T12:00:00+00:00' "
            "WHERE tenant_id='tenant-a' AND recommendation_id='rec-1'"
        )
        row = conn.execute(
            "SELECT state FROM product_recommendations WHERE tenant_id='tenant-a' AND recommendation_id='rec-1'"
        ).fetchone()
        assert row["state"] == "awaiting_review"


def test_v37_upgrades_from_v36_preserving_data(tmp_path) -> None:
    """v36 铺底升级到 v37，存量建议 + 审计保留（缺陷 6/3 数据安全）。"""
    db = Database(tmp_path / "v37-upgrade.sqlite3")
    _seed_v36(db)
    db.initialize()
    with db.connect() as conn:
        migrations = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        rec = conn.execute(
            "SELECT * FROM product_recommendations WHERE tenant_id='tenant-a' AND recommendation_id='rec-v36'"
        ).fetchone()
        audit = conn.execute(
            "SELECT * FROM product_recommendation_audit WHERE recommendation_id='rec-v36'"
        ).fetchone()
    assert 37 in migrations
    assert rec["rationale"] == "observe"
    assert rec["state"] == "draft"
    assert audit["action"] == "submit"


def test_v37_initialize_idempotent(tmp_path) -> None:
    """重复 initialize 幂等，37 迁移行只插一次。"""
    db = Database(tmp_path / "v37-idem.sqlite3")
    db.initialize()
    db.initialize()
    with db.connect() as conn:
        counts = dict(
            conn.execute(
                "SELECT version, COUNT(*) FROM schema_migrations WHERE version=37 GROUP BY version"
            ).fetchall()
        )
    assert counts.get(37) == 1
