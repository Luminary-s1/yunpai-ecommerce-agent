from __future__ import annotations

import importlib.util
from pathlib import Path

from ecommerce_agent.auth import Principal
from ecommerce_agent.service import AgentService

from conftest import make_settings


_RUNNER_PATH = (
    Path(__file__).resolve().parents[1] / "evals/performance/run_m4_latency.py"
)
_SPEC = importlib.util.spec_from_file_location("m4_latency_runner", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RUNNER)
profile = _RUNNER.profile


def test_latency_profile_measures_real_stream_time_to_first_delta(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    principal = Principal(
        tenant_id=service.settings.bootstrap_tenant_id,
        client_id=service.settings.bootstrap_client_id,
        subject_hash="latency-profile-test-subject",
        can_supply_order_context=False,
    )
    try:
        records = profile(service, principal)
    finally:
        service.close()

    generated = next(record for record in records if record["scenario"] == "product")
    assert generated["measurement_path"] == "service.chat_stream"
    assert generated["ttft_ms"] is not None
    assert 0 < generated["ttft_ms"] <= generated["total_ms"]
    assert generated["generation_first_delta_provider_ms"] is not None
    assert generated["generation_first_delta_provider_ms"] <= generated["ttft_ms"]
