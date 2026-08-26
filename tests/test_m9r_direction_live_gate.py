from __future__ import annotations

from types import SimpleNamespace

import pytest

from ecommerce_agent.product_workbench.eval import assert_direction_inputs_oracle_free
from ecommerce_agent.product_workbench.scenes import DIRECTION_SCENES, FrozenScene
from evals.product_lifecycle.run_m9r_direction_eval import (
    _require_live_settings,
)


def test_direction_eval_production_inputs_are_oracle_free() -> None:
    records = assert_direction_inputs_oracle_free(DIRECTION_SCENES)

    assert len(records) == len(DIRECTION_SCENES)
    assert all(record["production_input_sha256"] for record in records)


def test_direction_input_preflight_does_not_read_oracle() -> None:
    class _SceneWithGuardedOracle:
        name = "guarded"
        input_data = {"sku_id": "blind", "freshness": {"usable_as_current": True}}

        @property
        def expected(self):
            raise AssertionError("oracle read before model call")

    records = assert_direction_inputs_oracle_free([_SceneWithGuardedOracle()])

    assert records[0]["scene"] == "guarded"


def test_direction_eval_isolation_rejects_answer_value() -> None:
    leaked = FrozenScene(
        "leaked",
        input_data={
            "sku_id": "blind",
            "freshness": {"usable_as_current": True},
            "business_facts": {"target": "清仓预警"},
        },
        expected={"recommendation_type": "清仓预警"},
    )

    with pytest.raises(ValueError, match="oracle_value_in_production_input"):
        assert_direction_inputs_oracle_free([leaked])


@pytest.mark.parametrize(
    ("enabled", "mock_mode"),
    [(False, False), (True, True), (False, True)],
)
def test_live_direction_gate_rejects_disabled_or_mock_model(
    enabled: bool,
    mock_mode: bool,
) -> None:
    settings = SimpleNamespace(model_enabled=enabled, model_mock_mode=mock_mode)

    with pytest.raises(ValueError, match="enabled non-mock model"):
        _require_live_settings(settings)


def test_live_direction_gate_accepts_enabled_non_mock_model() -> None:
    settings = SimpleNamespace(model_enabled=True, model_mock_mode=False)

    _require_live_settings(settings)
