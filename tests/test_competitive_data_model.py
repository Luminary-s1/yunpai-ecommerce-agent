from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ecommerce_agent.business import (
    CompetitiveEntityMatchCreate,
    CompetitiveProductIdentity,
    CompetitorObservationCreate,
)
from ecommerce_agent.business.source_versioning import (
    SourceVersionError,
    canonical_source_time,
    payload_digest,
)
from ecommerce_agent.service import AgentService

from conftest import make_settings


def observation(**updates) -> CompetitorObservationCreate:
    values = {
        "connector_id": "licensed-feed",
        "store_id": "store-a",
        "subject_sku": "sku-a",
        "competitor_name": "竞店 A",
        "competitor_sku": "comp-a",
        "subject_price": Decimal("100"),
        "competitor_price": Decimal("90"),
        "currency": "CNY",
        "source_type": "licensed_provider",
        "source_ref": "https://licensed.example/observations/1",
        "source_id": "observation-source-1",
        "is_estimate": False,
        "observed_at": datetime(2026, 8, 5, 1, 0, tzinfo=UTC),
    }
    values.update(updates)
    return CompetitorObservationCreate(**values)


def product_identity(**updates) -> CompetitiveProductIdentity:
    values = {
        "title": "云湃智能客服一体机",
        "brand": "云湃",
        "model": "YP-100",
        "category": "智能客服一体机",
        "attributes": {"颜色": "曜石黑"},
    }
    values.update(updates)
    return CompetitiveProductIdentity(**values)


def entity_match(**updates) -> CompetitiveEntityMatchCreate:
    values = {
        "connector_id": "licensed-feed",
        "store_id": "store-a",
        "subject_sku": "sku-a",
        "competitor_name": "竞店 A",
        "competitor_sku": "comp-a",
        "subject_identity": product_identity(),
        "competitor_identity": product_identity(),
        "comparison_keys": ["颜色"],
        "source_type": "licensed_provider",
        "source_ref": "https://licensed.example/matches/1",
        "source_id": "entity-match-source-1",
        "is_estimate": False,
        "observed_at": datetime(2026, 8, 5, 1, 0, tzinfo=UTC),
    }
    values.update(updates)
    return CompetitiveEntityMatchCreate(**values)


@pytest.mark.parametrize(
    "updates",
    [
        {"rating_value": Decimal("4.5")},
        {"rating_scale": Decimal("5")},
        {"rating_value": Decimal("6"), "rating_scale": Decimal("5")},
        {"sales_rank": 3},
        {"rank_scope": "平台/类目/日榜"},
        {"sales_rank": 0, "rank_scope": "平台/类目/日榜"},
    ],
)
def test_observation_rejects_incomplete_or_invalid_rating_and_rank_pairs(updates) -> None:
    with pytest.raises(ValidationError) as exc_info:
        observation(**updates)
    assert all(error["type"] != "extra_forbidden" for error in exc_info.value.errors())


def test_observation_persists_normalizes_and_hashes_rating_and_rank(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    value = observation(
        rating_value=Decimal("9"),
        rating_scale=Decimal("10"),
        sales_rank=3,
        rank_scope="平台/智能客服一体机/日榜",
    )
    try:
        created = competitive.record("tenant-test", value)
        repeated = competitive.record("tenant-test", value)

        assert created["rating_value"] == "9"
        assert created["rating_scale"] == "10"
        assert created["normalized_rating"] == "4.50"
        assert created["sales_rank"] == 3
        assert created["rank_scope"] == "平台/智能客服一体机/日榜"
        assert repeated["id"] == created["id"]
        assert repeated["write_status"] == "idempotent"

        with pytest.raises(SourceVersionError, match="source_version_conflict"):
            competitive.record(
                "tenant-test",
                value.model_copy(update={"rating_value": Decimal("8")}),
            )
    finally:
        service.close()


@pytest.mark.parametrize(
    "omitted_fields",
    [
        (),
        ("entity_match_id",),
        ("rating_value", "rating_scale", "sales_rank", "rank_scope"),
        (
            "entity_match_id",
            "rating_value",
            "rating_scale",
            "sales_rank",
            "rank_scope",
        ),
    ],
)
def test_observation_replay_accepts_compatible_legacy_hash_field_sets(
    tmp_path,
    omitted_fields,
) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    value = observation()
    try:
        created = competitive.record("tenant-test", value)
        legacy_payload = value.model_dump(mode="json")
        legacy_payload["observed_at"] = canonical_source_time(value.observed_at)
        for field in omitted_fields:
            legacy_payload.pop(field)
        legacy_hash = payload_digest(legacy_payload)
        with service.db.connect() as conn:
            conn.execute(
                "UPDATE competitor_observations SET payload_hash=? WHERE id=?",
                (legacy_hash, created["id"]),
            )

        repeated = competitive.record("tenant-test", value)

        assert repeated["id"] == created["id"]
        assert repeated["write_status"] == "idempotent"
        with service.db.connect() as conn:
            stored_hash = conn.execute(
                "SELECT payload_hash FROM competitor_observations WHERE id=?",
                (created["id"],),
            ).fetchone()[0]
        assert stored_hash == legacy_hash
    finally:
        service.close()


def test_observation_legacy_hash_rejects_nonempty_v26_facts(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    value = observation()
    try:
        created = competitive.record("tenant-test", value)
        legacy_payload = value.model_dump(mode="json")
        legacy_payload["observed_at"] = canonical_source_time(value.observed_at)
        for field in ("rating_value", "rating_scale", "sales_rank", "rank_scope"):
            legacy_payload.pop(field)
        with service.db.connect() as conn:
            conn.execute(
                "UPDATE competitor_observations SET payload_hash=? WHERE id=?",
                (payload_digest(legacy_payload), created["id"]),
            )

        with pytest.raises(SourceVersionError, match="source_version_conflict"):
            competitive.record(
                "tenant-test",
                value.model_copy(
                    update={
                        "rating_value": Decimal("4.5"),
                        "rating_scale": Decimal("5"),
                    }
                ),
            )
    finally:
        service.close()


def test_entity_match_replay_accepts_legacy_hash_without_custom_dimensions(
    tmp_path,
) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    value = entity_match()
    try:
        created = competitive.record_entity_match("tenant-test", value)
        legacy_payload = value.model_dump(mode="json")
        legacy_payload["observed_at"] = canonical_source_time(value.observed_at)
        assert legacy_payload["subject_identity"].pop("custom_dimensions") == []
        assert legacy_payload["competitor_identity"].pop("custom_dimensions") == []
        legacy_hash = payload_digest(legacy_payload)
        with service.db.connect() as conn:
            conn.execute(
                "UPDATE competitive_entity_matches SET payload_hash=? WHERE id=?",
                (legacy_hash, created["id"]),
            )

        repeated = competitive.record_entity_match("tenant-test", value)

        assert repeated["id"] == created["id"]
        assert repeated["write_status"] == "idempotent"
        with service.db.connect() as conn:
            stored_hash = conn.execute(
                "SELECT payload_hash FROM competitive_entity_matches WHERE id=?",
                (created["id"],),
            ).fetchone()[0]
        assert stored_hash == legacy_hash
    finally:
        service.close()


def test_entity_match_legacy_hash_rejects_nonempty_custom_dimensions(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    value = entity_match()
    try:
        created = competitive.record_entity_match("tenant-test", value)
        legacy_payload = value.model_dump(mode="json")
        legacy_payload["observed_at"] = canonical_source_time(value.observed_at)
        legacy_payload["subject_identity"].pop("custom_dimensions")
        legacy_payload["competitor_identity"].pop("custom_dimensions")
        with service.db.connect() as conn:
            conn.execute(
                "UPDATE competitive_entity_matches SET payload_hash=? WHERE id=?",
                (payload_digest(legacy_payload), created["id"]),
            )

        changed = value.model_copy(
            update={
                "competitor_identity": product_identity(
                    custom_dimensions=[
                        {
                            "key": "memory_gb",
                            "label": "内存",
                            "value_type": "number",
                            "value_number": Decimal("32"),
                            "unit": "GB",
                        }
                    ]
                )
            }
        )
        with pytest.raises(
            ValueError,
            match="competitive_match_version_conflict",
        ):
            competitive.record_entity_match("tenant-test", changed)
    finally:
        service.close()


def test_observation_source_id_tie_uses_latest_created_record(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    value = observation()
    try:
        older = competitive.record(
            "tenant-test",
            value.model_copy(update={"competitor_sku": "aaa-older"}),
        )
        current_payload = value.model_dump(mode="json")
        current_payload["observed_at"] = canonical_source_time(value.observed_at)
        with service.db.connect() as conn:
            conn.execute(
                "UPDATE competitor_observations SET created_at=? WHERE id=?",
                ("2026-08-05T01:01:00+00:00", older["id"]),
            )
            conn.execute(
                """
                INSERT INTO competitor_observations(
                    id, tenant_id, connector_id, store_id, subject_sku,
                    competitor_name, competitor_sku, subject_price, competitor_price,
                    currency, rating_value, rating_scale, sales_rank, rank_scope,
                    source_type, source_ref, is_estimate, observed_at, source_id,
                    created_at, payload_hash, entity_match_id
                )
                SELECT ?, tenant_id, connector_id, store_id, subject_sku,
                       competitor_name, ?, subject_price, competitor_price,
                       currency, rating_value, rating_scale, sales_rank, rank_scope,
                       source_type, source_ref, is_estimate, observed_at, source_id,
                       ?, ?, entity_match_id
                FROM competitor_observations WHERE id=?
                """,
                (
                    "competitor-latest-created",
                    value.competitor_sku,
                    "2026-08-05T01:02:00+00:00",
                    payload_digest(current_payload),
                    older["id"],
                ),
            )

        repeated = competitive.record("tenant-test", value)

        assert repeated["id"] == "competitor-latest-created"
        assert repeated["write_status"] == "idempotent"
    finally:
        service.close()


def test_observation_source_id_applies_newer_and_rejects_stale_versions(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    first_value = observation()
    try:
        first = competitive.record("tenant-test", first_value)
        newer = competitive.record(
            "tenant-test",
            first_value.model_copy(
                update={
                    "competitor_price": Decimal("88"),
                    "observed_at": first_value.observed_at + timedelta(hours=1),
                }
            ),
        )

        assert first["write_status"] == "applied"
        assert newer["write_status"] == "applied"
        assert newer["id"] != first["id"]
        assert newer["competitor_price"] == "88"

        with pytest.raises(SourceVersionError, match="stale_source_version"):
            competitive.record(
                "tenant-test",
                first_value.model_copy(
                    update={
                        "competitor_price": Decimal("92"),
                        "observed_at": first_value.observed_at - timedelta(minutes=1),
                    }
                ),
            )
    finally:
        service.close()


@pytest.mark.parametrize(
    "custom_dimensions",
    [
        [
            {
                "key": "memory_gb",
                "label": "内存",
                "value_type": "number",
                "value_number": Decimal("32"),
                "unit": "GB",
            },
            {
                "key": "MEMORY_GB",
                "label": "内存容量",
                "value_type": "number",
                "value_number": Decimal("64"),
                "unit": "GB",
            },
        ],
        [
            {
                "key": "deployment",
                "label": "部署方式",
                "value_type": "text",
                "value_text": "本地",
                "value_number": Decimal("1"),
            }
        ],
        [
            {
                "key": "supports_voice",
                "label": "支持语音",
                "value_type": "boolean",
            }
        ],
        [
            {
                "key": "deployment",
                "label": "部署方式",
                "value_type": "text",
                "value_text": "本地",
                "unit": "套",
            }
        ],
    ],
)
def test_custom_dimensions_reject_duplicate_or_mismatched_typed_values(
    custom_dimensions,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        product_identity(custom_dimensions=custom_dimensions)
    assert all(error["type"] != "extra_forbidden" for error in exc_info.value.errors())


def test_custom_dimensions_are_typed_and_persisted_with_entity_identity(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    competitive = service.operations.competitive
    dimensions = [
        {
            "key": "deployment",
            "label": "部署方式",
            "value_type": "text",
            "value_text": "本地",
        },
        {
            "key": "memory_gb",
            "label": "内存",
            "value_type": "number",
            "value_number": Decimal("32"),
            "unit": "GB",
        },
        {
            "key": "supports_voice",
            "label": "支持语音",
            "value_type": "boolean",
            "value_boolean": True,
        },
    ]
    payload = CompetitiveEntityMatchCreate(
        connector_id="licensed-feed",
        store_id="store-a",
        subject_sku="sku-a",
        competitor_name="竞店 A",
        competitor_sku="comp-a",
        subject_identity=product_identity(),
        competitor_identity=product_identity(custom_dimensions=dimensions),
        comparison_keys=["颜色"],
        source_type="licensed_provider",
        source_ref="https://licensed.example/matches/typed-dimensions",
        source_id="typed-dimensions-1",
        is_estimate=False,
        observed_at=datetime(2026, 8, 5, 1, 0, tzinfo=UTC),
    )
    try:
        created = competitive.record_entity_match("tenant-test", payload)
        assert created["competitor_identity"]["custom_dimensions"] == [
            {
                "key": "deployment",
                "label": "部署方式",
                "value_type": "text",
                "value_text": "本地",
                "value_number": None,
                "value_boolean": None,
                "unit": None,
            },
            {
                "key": "memory_gb",
                "label": "内存",
                "value_type": "number",
                "value_text": None,
                "value_number": "32",
                "value_boolean": None,
                "unit": "GB",
            },
            {
                "key": "supports_voice",
                "label": "支持语音",
                "value_type": "boolean",
                "value_text": None,
                "value_number": None,
                "value_boolean": True,
                "unit": None,
            },
        ]
    finally:
        service.close()
