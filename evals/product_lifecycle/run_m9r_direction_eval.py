"""Run the M9-R direction-discovery gate with the configured production model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from ecommerce_agent.config import Settings
from ecommerce_agent.llm import ModelGateway
from ecommerce_agent.product_diagnosis.interpreter import DiagnosisModelInterpreter
from ecommerce_agent.product_lifecycle.engine import RecommendationModelInterpreter
from ecommerce_agent.product_workbench.eval import (
    MechanismEvalRunner,
    assert_direction_inputs_oracle_free,
)
from ecommerce_agent.product_workbench.scenes import DIRECTION_SCENES


def _load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        values = shlex.split(raw_value, comments=True, posix=True)
        os.environ[key] = values[0] if values else ""


def _require_live_settings(settings: Settings) -> None:
    if not settings.model_enabled or settings.model_mock_mode:
        raise ValueError("live M9 direction eval requires enabled non-mock model")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    _load_env_file(args.env_file)
    configured_settings = Settings.from_env()
    try:
        _require_live_settings(configured_settings)
        isolation = assert_direction_inputs_oracle_free(DIRECTION_SCENES)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    settings = replace(configured_settings, model_temperature=0.0)
    gateway = ModelGateway(settings)
    healthy, reason = gateway.health()
    if not healthy:
        gateway.close()
        raise SystemExit(f"model gateway is not configured: {reason}")

    try:
        runner = MechanismEvalRunner(
            scenes=DIRECTION_SCENES,
            interpreter=DiagnosisModelInterpreter(gateway),
            recommendation_interpreter=RecommendationModelInterpreter(gateway),
        )
        results = runner.run_all()
    finally:
        gateway.close()

    oracle_isolation = []
    for scene, input_record in zip(DIRECTION_SCENES, isolation, strict=True):
        oracle_text = json.dumps(scene.expected, ensure_ascii=False, sort_keys=True)
        oracle_isolation.append({
            **input_record,
            "oracle_sha256": hashlib.sha256(oracle_text.encode()).hexdigest(),
        })

    report = {
        "schema_version": "m9r-live-direction-eval-v2",
        "run_at": datetime.now(UTC).isoformat(),
        "mode": "live",
        "provider": settings.model_provider,
        "model": settings.model_name,
        "configured_temperature": configured_settings.model_temperature,
        "evaluation_temperature": settings.model_temperature,
        "passed": all(result.passed for result in results),
        "passed_count": sum(result.passed for result in results),
        "total_count": len(results),
        "oracle_read_after_model_call": True,
        "production_input_oracle_separated": True,
        "oracle_isolation": oracle_isolation,
        "records": [
            {
                "scene": result.scene_name,
                "passed": result.passed,
                "failures": result.failures,
                "produced": result.produced,
            }
            for result in results
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "passed": report["passed"],
        "passed_count": report["passed_count"],
        "total_count": report["total_count"],
    }, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
