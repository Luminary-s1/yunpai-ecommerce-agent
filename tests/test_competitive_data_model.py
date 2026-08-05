from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ecommerce_agent.business import CompetitorObservationCreate
from ecommerce_agent.business.source_versioning import SourceVersionError
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
