from __future__ import annotations

from dataclasses import replace

from scripts.run_customer_eval import _profile_rank, _tuning_steps

from conftest import make_settings


def _run(*, gate: bool, answer: float, hallucination: float) -> dict:
    return {
        "gate": {"passed": gate},
        "metrics": {
            "answer_accuracy": answer,
            "hallucination_rate": hallucination,
            "refusal_rate": 0.0,
            "pass_rate": answer,
            "model_fallback_rate": 0.0,
        },
    }


def test_profile_rank_keeps_a_passing_baseline_over_a_regressing_tune() -> None:
    baseline = _run(gate=True, answer=0.80, hallucination=0.04)
    regression = _run(gate=False, answer=0.82, hallucination=0.14)

    assert _profile_rank(baseline) > _profile_rank(regression)


def test_final_live_settings_do_not_schedule_an_unproven_rag_change(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        model_enabled=True,
        model_api_key="test-key",
        model_max_output_tokens=1600,
        model_streaming=False,
    )

    assert _tuning_steps(settings, "live") == []
