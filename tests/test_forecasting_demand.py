from __future__ import annotations

from ecommerce_agent.database import Database


def test_v28_database_upgrades_to_v29_with_forecasting_contract(tmp_path) -> None:
    db = Database(tmp_path / "v28-forecasting.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 29):
            getattr(Database, f"_apply_v{version}")(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-08-11T00:00:00+00:00"),
            )
        conn.execute("CREATE TABLE legacy_probe(id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO legacy_probe VALUES ('probe-1', 'preserved')")

    db.initialize()
    db.initialize()

    with db.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        migrations = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
        migration_count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=29"
        ).fetchone()[0]
        fact_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(demand_daily_facts)")
        }
        probe = conn.execute(
            "SELECT value FROM legacy_probe WHERE id='probe-1'"
        ).fetchone()[0]

    assert Database.SCHEMA_VERSION >= 29
    assert 29 in migrations
    assert migration_count == 1
    assert {
        "demand_daily_facts",
        "forecast_policies",
        "forecast_runs",
        "forecast_backtests",
        "forecast_points",
        "forecast_anomalies",
    } <= tables
    assert {
        "tenant_id",
        "store_id",
        "sku_id",
        "business_date",
        "gross_units",
        "eligible_units",
        "price",
        "promotion_flag",
        "source_watermark",
        "fact_version",
        "demand_policy_version",
        "quality_flags_json",
        "payload_hash",
    } <= fact_columns
    assert probe == "preserved"
