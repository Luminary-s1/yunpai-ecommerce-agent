#!/usr/bin/env python3
"""Run the frozen WP4 customer-service evaluation in an isolated database.

The command intentionally uses the existing ``AgentService.run_evaluation_suite``
path.  That method snapshots the prepared database before creating the evaluation
service, so the production/primary ``sessions``, ``messages`` and handoff tables
remain untouched.  ``--mode both`` records mock and live results in one redacted
JSON report; a live provider being unavailable is reported as such rather than
being silently counted as a prediction.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import sys
import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ecommerce_agent.config import Settings  # noqa: E402
from ecommerce_agent.evaluation import (  # noqa: E402
    EvaluationCaseCreate,
    EvaluationCaseReplaceRequest,
    EvaluationRunRequest,
    EvaluationSuiteCreateRequest,
    EvaluationSuiteTransition,
)
from ecommerce_agent.knowledge_management import (  # noqa: E402
    KnowledgeCreateRequest,
    KnowledgeTransitionRequest,
)
from ecommerce_agent.service import AgentService  # noqa: E402
from ecommerce_agent.simulation import VirtualStoreSimulation  # noqa: E402


EVAL_FIXTURE = ROOT / "src/ecommerce_agent/fixtures/customer_service_eval_v1.json"
STORE_FIXTURE = ROOT / "src/ecommerce_agent/fixtures/virtual_store_v1.json"
DEFAULT_OUT = ROOT / "evals/customer_service/runs/customer-service-eval-latest.json"
TENANT_ID = "customer-eval-wp4"
ACTOR = "customer-eval-script"


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"fixture must contain an object: {path}")
    return value


def _load_env_file(path: Path) -> int:
    """Load export assignments from env.md without printing their values."""

    loaded = 0
    in_code_block = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            in_code_block = not in_code_block
            continue
        if not in_code_block and not line.startswith("export "):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line or line.startswith("#"):
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        try:
            value = shlex.split(raw_value, comments=True, posix=True)
        except ValueError:
            continue
        os.environ[key] = value[0] if value else ""
        loaded += 1
    return loaded


def _evaluation_settings(
    base: Settings,
    *,
    mode: str,
    data_dir: Path,
    tuning: Mapping[str, Any] | None = None,
) -> Settings:
    """Build an evaluation-only settings copy; production settings are untouched."""

    values: dict[str, Any] = {
        "data_dir": data_dir,
        "backup_dir": None,
        "model_enabled": True,
        "model_mock_mode": mode == "mock",
        "outbox_worker_enabled": False,
        "channel_agent_worker_enabled": False,
        "taobao_auto_reply_enabled": False,
        "release_gate_required": False,
        "competitive_monitor_worker_enabled": False,
        "handoff_dispatch_worker_enabled": False,
        "handoff_sla_worker_enabled": False,
    }
    if tuning:
        values.update(tuning)
    return replace(base, **values)


def _load_virtual_store(service: AgentService) -> None:
    fixture = _read_json(STORE_FIXTURE)
    if fixture.get("virtual") is not True:
        raise ValueError("the evaluation store fixture must be explicitly virtual")
    VirtualStoreSimulation(service)._load_store_data(
        fixture, tenant_id=TENANT_ID, actor=ACTOR
    )


def _load_evaluation_knowledge(
    service: AgentService, fixture: Mapping[str, Any]
) -> dict[str, int]:
    """Insert and approve the fixture's source documents in the isolated tenant."""

    existing = service.knowledge_management.list_items(TENANT_ID, limit=500)
    by_source = {str(item["source"]): item for item in existing}
    counts: Counter[str] = Counter()
    store_id = "qingchuan-flagship-001"
    for document in fixture["knowledge"]:
        source = str(document["source"])
        item = by_source.get(source)
        if item is None:
            item = service.knowledge_management.create(
                TENANT_ID,
                KnowledgeCreateRequest.model_validate(
                    {**document, "store_id": document.get("store_id", store_id)}
                ),
                ACTOR,
            )
            counts["created"] += 1
        else:
            counts["reused"] += 1
        if item["status"] == "candidate" and item["review_status"] == "draft":
            item = service.knowledge_management.evaluate(
                TENANT_ID,
                item["id"],
                KnowledgeTransitionRequest(
                    expected_record_version=item["record_version"],
                    note="WP4 virtual evaluation source review",
                ),
                ACTOR,
            )
            counts["evaluated"] += 1
        if item["status"] == "candidate" and item["review_status"] == "evaluated":
            service.knowledge_management.approve(
                TENANT_ID,
                item["id"],
                KnowledgeTransitionRequest(
                    expected_record_version=item["record_version"],
                    note="WP4 isolated virtual evaluation source approval",
                ),
                ACTOR,
            )
            counts["approved"] += 1
    return dict(sorted(counts.items()))


def _prepare_suite(service: AgentService, fixture: Mapping[str, Any]) -> dict[str, Any]:
    request = EvaluationSuiteCreateRequest.model_validate(fixture["suite"])
    suite = service.evaluations.create_suite(TENANT_ID, request, ACTOR)
    cases = [EvaluationCaseCreate.model_validate(case) for case in fixture["cases"]]
    suite = service.evaluations.replace_cases(
        TENANT_ID,
        suite["id"],
        EvaluationCaseReplaceRequest(
            expected_record_version=suite["record_version"], cases=cases
        ),
        ACTOR,
    )
    return service.evaluations.freeze_suite(
        TENANT_ID,
        suite["id"],
        EvaluationSuiteTransition(
            expected_record_version=suite["record_version"],
            note="WP4 D20 frozen fixture run",
        ),
        ACTOR,
    )


def _primary_counts(service: AgentService) -> dict[str, int]:
    counts: dict[str, int] = {}
    with service.db.connect() as conn:
        for table in ("sessions", "messages", "handoff_tasks"):
            counts[table] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE tenant_id=?", (TENANT_ID,)
                ).fetchone()[0]
            )
    return counts


def _failure_attribution(violations: list[str]) -> str:
    codes = {item.split(":", 1)[-1] for item in violations}
    if any("context" in code or "readiness" in code for code in codes):
        return "truncation_or_context_loss"
    if any(
        code
        in {
            "intent_mismatch",
            "missed_handoff",
            "unexpected_handoff",
            "missed_refusal",
            "unexpected_refusal",
            "risk_above_expected",
        }
        for code in codes
    ):
        return "intent_or_handoff_routing"
    if any(
        "evidence" in code or "grounding" in code or code == "unsupported_grounded_claim"
        for code in codes
    ):
        return "retrieval_or_source_coverage"
    return "prompt_or_answer_contract"


def _compact_run(run: Mapping[str, Any]) -> dict[str, Any]:
    results = list(run.get("results", []))
    failed = [
        {
            "case_key": result["case_key"],
            "scenario": result["scenario"],
            "violations": result.get("violations", []),
            "attribution": _failure_attribution(result.get("violations", [])),
            "actual": [
                {
                    key: turn.get(key)
                    for key in (
                        "turn",
                        "intent",
                        "requires_human",
                        "reason",
                        "source_count",
                        "model_fallback",
                        "context_readiness",
                        "is_refusal",
                        "hallucinated",
                        "answer_excerpt",
                        "violations",
                    )
                    if key in turn
                }
                for turn in result.get("actual", {}).get("turns", [])
                if turn.get("violations")
            ],
        }
        for result in results
        if not result.get("passed")
    ]
    attribution = Counter(item["attribution"] for item in failed)
    return {
        "run_id": run["id"],
        "run_key": run["run_key"],
        "status": run["status"],
        "metrics": run["metrics"],
        "gate": run["gate"],
        "failed_cases": failed,
        "failure_attribution": dict(sorted(attribution.items())),
    }


def _counterexample(service: AgentService, raw_case: Mapping[str, Any]) -> dict[str, Any]:
    """Prove the forbidden-term detector is causally active, then discard the edit."""

    original = copy.deepcopy(raw_case)
    mutated = copy.deepcopy(raw_case)
    labeled = next(
        turn for turn in reversed(mutated["turns"]) if turn.get("expectation")
    )
    terms = list(labeled["expectation"].get("forbidden_answer_terms", []))
    if not terms:
        raise ValueError("adversarial counterexample has no forbidden terms")
    answer = terms[0] + "（synthetic counterexample）"
    response = SimpleNamespace(
        answer=answer,
        intent="chitchat",
        risk_level="low",
        requires_human=False,
        reason="prompt_injection",
        sources=[],
        model_fallback=False,
        context_readiness="ready",
    )
    original_case = service.evaluations._prepare_case(
        EvaluationCaseCreate.model_validate(original)
    )
    original_case["id"] = "counterexample-original"
    original_result = service.evaluations._evaluate_case(
        original_case, [response for _ in original_case["turns"]], None
    )
    labeled["expectation"]["forbidden_answer_terms"] = []
    mutated_case = service.evaluations._prepare_case(
        EvaluationCaseCreate.model_validate(mutated)
    )
    mutated_case["id"] = "counterexample-without-forbidden-term"
    mutated_result = service.evaluations._evaluate_case(
        mutated_case, [response for _ in mutated_case["turns"]], None
    )
    original_metrics = service.evaluations._metrics([original_result], {})
    mutated_metrics = service.evaluations._metrics([mutated_result], {})
    changed = original_metrics["hallucination_rate"] != mutated_metrics["hallucination_rate"]
    if not changed:
        raise AssertionError("removing forbidden terms did not change hallucination rate")
    return {
        "case_key": raw_case["case_key"],
        "term_removed": terms[0],
        "with_forbidden_terms": original_metrics["hallucination_rate"],
        "without_forbidden_terms": mutated_metrics["hallucination_rate"],
        "changed": changed,
        "restored": True,
    }


def _tuning_steps(base: Settings, mode: str) -> list[dict[str, Any]]:
    """Return cumulative, one-variable-at-a-time tuning steps.

    DeepSeek's reasoning responses exposed two separate transport constraints in
    the first live run: a small output budget could exhaust before ``content``
    was emitted, and streaming could expose a reasoning-only response.  Keeping
    these as two stages makes each delta attributable instead of hiding a pair
    of changes under one "tuned" result.
    """

    steps: list[dict[str, Any]] = []
    if mode == "live":
        if base.model_max_output_tokens < 1600:
            steps.append(
                {
                    "parameter": "model_max_output_tokens",
                    "baseline": base.model_max_output_tokens,
                    "tuned": 1600,
                    "setting": {"model_max_output_tokens": 1600},
                }
            )
        if base.model_streaming:
            steps.append(
                {
                    "parameter": "model_streaming",
                    "baseline": True,
                    "tuned": False,
                    "setting": {"model_streaming": False},
                }
            )
        if steps:
            return steps
        return []
    if base.rag_min_score > 0.05:
        return [
            {
                "parameter": "rag_min_score",
                "baseline": base.rag_min_score,
                "tuned": 0.05,
                "setting": {"rag_min_score": 0.05},
            }
        ]
    tuned_top_k = min(base.rag_top_k + 2, 10)
    return [
        {
            "parameter": "rag_top_k",
            "baseline": base.rag_top_k,
            "tuned": tuned_top_k,
            "setting": {"rag_top_k": tuned_top_k},
        }
    ]


def _profile_rank(run: Mapping[str, Any]) -> tuple[Any, ...]:
    metrics = run["metrics"]
    primary_targets_met = (
        metrics["answer_accuracy"] >= 0.75
        and metrics["hallucination_rate"] <= 0.10
    )
    return (
        bool(run["gate"].get("passed")),
        primary_targets_met,
        metrics["hallucination_rate"] <= 0.10,
        metrics["answer_accuracy"],
        -metrics["hallucination_rate"],
        -metrics["refusal_rate"],
        metrics["pass_rate"],
        -metrics["model_fallback_rate"],
    )


def _copy_snapshot(source_dir: Path, destination_dir: Path) -> None:
    destination_dir.mkdir()
    (destination_dir / "agent.sqlite3").write_bytes(
        (source_dir / "agent.sqlite3").read_bytes()
    )
    checkpoint = source_dir / "checkpoints.sqlite3"
    if checkpoint.exists():
        (destination_dir / "checkpoints.sqlite3").write_bytes(checkpoint.read_bytes())


def _run_mode(mode: str, base: Settings, fixture: Mapping[str, Any]) -> dict[str, Any]:
    if mode == "live":
        ok = base.model_enabled and bool(base.model_api_key)
        if not ok:
            return {"status": "unavailable", "reason": "model_not_configured"}
        # Do not make a probe request: a provider may enforce a one-request quota.
    tuning_steps = _tuning_steps(base, mode)
    with tempfile.TemporaryDirectory(prefix=f"customer-eval-{mode}-", dir=base.data_dir.parent) as tmp:
        primary_dir = Path(tmp) / "primary"
        primary_dir.mkdir()
        settings = _evaluation_settings(base, mode=mode, data_dir=primary_dir)
        service = AgentService(settings)
        try:
            _load_virtual_store(service)
            loaded_knowledge = _load_evaluation_knowledge(service, fixture)
            suite = _prepare_suite(service, fixture)
            before = _primary_counts(service)
            baseline = service.run_evaluation_suite(
                TENANT_ID,
                suite["id"],
                EvaluationRunRequest(run_key=f"wp4-d20-{mode}-baseline"),
                ACTOR,
            )
            after_baseline = _primary_counts(service)
            counterexample_case = next(
                case
                for case in fixture["cases"]
                if case["scenario"] == "adversarial"
                and any(
                    turn.get("expectation", {}).get("forbidden_answer_terms")
                    for turn in case["turns"]
                )
            )
            counterexample = _counterexample(service, counterexample_case)
        except Exception as exc:
            return {
                "status": "error",
                "error_type": type(exc).__name__,
                "primary_counts_before": before if "before" in locals() else None,
                "primary_counts_after": after_baseline if "after_baseline" in locals() else None,
            }
        finally:
            service.close()
        baseline_compact = _compact_run(baseline)
        previous_compact = baseline_compact
        selected_profile = "baseline"
        selected_compact = baseline_compact
        selected_settings: dict[str, Any] = {}
        cumulative_settings: dict[str, Any] = {}
        stages: list[dict[str, Any]] = []
        stage_counts: list[dict[str, Any]] = []
        for index, step in enumerate(tuning_steps, start=1):
            cumulative_settings.update(step["setting"])
            stage_dir = Path(tmp) / f"stage-{index}"
            _copy_snapshot(primary_dir, stage_dir)
            stage_service = AgentService(
                _evaluation_settings(
                    base,
                    mode=mode,
                    data_dir=stage_dir,
                    tuning=cumulative_settings,
                )
            )
            try:
                stage_before = _primary_counts(stage_service)
                stage_run = stage_service.run_evaluation_suite(
                    TENANT_ID,
                    suite["id"],
                    EvaluationRunRequest(
                        run_key=f"wp4-d20-{mode}-tuned-{index}",
                        baseline_run_id=baseline["id"],
                    ),
                    ACTOR,
                )
                stage_after = _primary_counts(stage_service)
            except Exception as exc:
                return {
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "baseline": baseline_compact,
                    "tuning_steps": stages,
                    "primary_counts_before": before,
                    "primary_counts_after_baseline": after_baseline,
                }
            finally:
                stage_service.close()
            stage_compact = _compact_run(stage_run)
            deltas = {
                key: stage_compact["metrics"].get(key, 0)
                - previous_compact["metrics"].get(key, 0)
                for key in (
                    "answer_accuracy",
                    "hallucination_rate",
                    "refusal_rate",
                    "pass_rate",
                    "evidence_coverage",
                )
            }
            stages.append(
                {
                    "profile": f"stage-{index}",
                    "parameter": step["parameter"],
                    "baseline": step["baseline"],
                    "tuned": step["tuned"],
                    "metric_deltas_from_previous": deltas,
                    "settings": dict(cumulative_settings),
                    "run": stage_compact,
                }
            )
            stage_counts.append({"before": stage_before, "after": stage_after})
            previous_compact = stage_compact
            if _profile_rank(stage_compact) > _profile_rank(selected_compact):
                selected_profile = f"stage-{index}"
                selected_compact = stage_compact
                selected_settings = dict(cumulative_settings)
        tuned_compact = selected_compact
        final_deltas = {
            key: tuned_compact["metrics"].get(key, 0)
            - baseline_compact["metrics"].get(key, 0)
            for key in (
                "answer_accuracy",
                "hallucination_rate",
                "refusal_rate",
                "pass_rate",
                "evidence_coverage",
            )
        }
        all_isolated = before == after_baseline and all(
            item["before"] == item["after"] for item in stage_counts
        )
        return {
            "status": "completed",
            "model": {
                "provider": base.model_provider,
                "name": base.model_name,
                "mock": mode == "mock",
            },
            "fixture": {
                "fixture_id": fixture["fixture_id"],
                "suite_key": fixture["suite"]["suite_key"],
                "case_count": len(fixture["cases"]),
                "knowledge_count": len(fixture["knowledge"]),
                "dataset_hash": suite["dataset_hash"],
                "loaded_knowledge": loaded_knowledge,
            },
            "primary_isolation": {
                "before": before,
                "after_baseline": after_baseline,
                "stages": stage_counts,
                "zero_new_sessions_messages_handoffs": all_isolated,
            },
            "baseline": baseline_compact,
            "tuned": tuned_compact,
            "tuning": {
                "steps": stages,
                "metric_deltas_from_baseline": final_deltas,
                "selected_profile": selected_profile,
                "attempted_final_settings": dict(cumulative_settings),
                "final_settings": selected_settings,
            },
            "forbidden_term_counterexample": counterexample,
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("mock", "live", "both"), default="both")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--env-file", type=Path, help="optional env.md export block")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.env_file:
        _load_env_file(args.env_file)
    fixture = _read_json(EVAL_FIXTURE)
    base = Settings.from_env()
    modes = ("mock", "live") if args.mode == "both" else (args.mode,)
    report: dict[str, Any] = {
        "report_version": "customer-service-eval-report-v1",
        "fixture_path": str(EVAL_FIXTURE.relative_to(ROOT)),
        "modes": {},
    }
    exit_code = 0
    for mode in modes:
        result = _run_mode(mode, base, fixture)
        report["modes"][mode] = result
        if result.get("status") != "completed":
            exit_code = 1
        elif not result["tuned"]["gate"].get("passed", False):
            exit_code = 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for mode, result in report["modes"].items():
        if result.get("status") != "completed":
            print(f"{mode}: {result['status']} ({result.get('reason', result.get('error_type', 'unknown'))})")
            continue
        metrics = result["tuned"]["metrics"]
        print(
            f"{mode}: status={result['tuned']['status']} gate={result['tuned']['gate']['passed']} "
            f"answer_accuracy={metrics['answer_accuracy']:.3f} "
            f"hallucination_rate={metrics['hallucination_rate']:.3f} "
            f"refusal_rate={metrics['refusal_rate']:.3f} "
            f"isolated={result['primary_isolation']['zero_new_sessions_messages_handoffs']}"
        )
    print(f"report: {args.out}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
