from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .auth import Principal
from .evaluation import (
    EvaluationCaseCreate,
    EvaluationCaseReplaceRequest,
    EvaluationRunRequest,
    EvaluationSuiteCreateRequest,
    EvaluationSuiteReviseRequest,
    EvaluationSuiteTransition,
)
from .schemas import ChatResponse


ROOT = Path(__file__).resolve().parents[2]
INPUT_PATH = ROOT / "evals/customer_service/m8r_wp4_inputs_v1.json"
ORACLE_PATH = ROOT / "evals/customer_service/m8r_wp4_oracle_v1.json"
SHADOW_SOURCE_PREFIX = "m8r-wp4-shadow:"


class ShadowRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_key: str = Field(
        min_length=3,
        max_length=96,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )


class ShadowFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal[-1, 1]
    corrected_answer: str | None = Field(default=None, max_length=1200)
    note: str | None = Field(default=None, max_length=1000)
    evidence_source: str | None = Field(default=None, min_length=4, max_length=500)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"fixture must contain an object: {path}")
    return value


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_m8r_eval_definition() -> dict[str, Any]:
    inputs = _read_json(INPUT_PATH)
    oracle = _read_json(ORACLE_PATH)
    if inputs.get("fixture_id") != oracle.get("fixture_id"):
        raise ValueError("M8-R input and oracle fixture ids do not match")
    if inputs.get("virtual") is not True:
        raise ValueError("M8-R workbench fixture must be explicitly virtual")

    oracle_cases = {str(item["case_key"]): item for item in oracle.get("cases", [])}
    input_cases = list(inputs.get("cases", []))
    if {str(item["case_key"]) for item in input_cases} != set(oracle_cases):
        raise ValueError("M8-R input and oracle case keys do not match")

    joined: list[dict[str, Any]] = []
    evaluation_cases: list[dict[str, Any]] = []
    forbidden_input_keys = {
        "expectation",
        "expected_intent",
        "expected_requires_human",
        "expected_reason",
        "expected_refusal",
        "required_answer_terms",
        "forbidden_answer_terms",
    }
    for raw in input_cases:
        encoded = json.dumps(raw, ensure_ascii=False)
        if any(f'"{key}"' in encoded for key in forbidden_input_keys):
            raise ValueError("M8-R production input contains oracle fields")
        case_key = str(raw["case_key"])
        expected_turns = list(oracle_cases[case_key].get("turns", []))
        input_turns = list(raw.get("turns", []))
        if len(input_turns) != len(expected_turns):
            raise ValueError(f"M8-R turn count mismatch: {case_key}")
        turns = []
        for turn, expectation in zip(input_turns, expected_turns, strict=True):
            item = {"message": turn["message"], "context": turn.get("context", {})}
            if expectation is not None:
                item["expectation"] = expectation
            turns.append(item)
        scenario = f"{raw['scenario']}.{raw['partition']}"
        evaluation_case = {
            "case_key": case_key,
            "scenario": scenario,
            "source_ref": raw.get("source_ref", ""),
            "turns": turns,
        }
        evaluation_cases.append(evaluation_case)
        joined.append(
            {
                **raw,
                "scenario": scenario,
                "oracle": {"turns": expected_turns},
            }
        )

    input_hash = _digest(inputs)
    oracle_hash = _digest(oracle)
    return {
        "contract_version": "m8r-customer-service-workbench-v1",
        "fixture_id": inputs["fixture_id"],
        "virtual": True,
        "suite": inputs["suite"],
        "cases": joined,
        "evaluation_cases": evaluation_cases,
        "input_hash": input_hash,
        "oracle_hash": oracle_hash,
        "runner_contract": {
            "case_fields": ["case_key", "scenario", "source_ref", "turns"],
            "turn_fields": ["message", "context"],
            "oracle_fields_visible_to_runner": [],
        },
    }


def ensure_m8r_frozen_suite(service: Any, tenant_id: str, actor: str) -> dict[str, Any]:
    definition = load_m8r_eval_definition()
    suite_payload = dict(definition["suite"])
    source_ref = (
        f"m8r-wp4:input:{definition['input_hash']}:oracle:{definition['oracle_hash']}"
    )
    suite_payload.update({"source_ref": source_ref, "deidentified": True})
    matching = [
        item
        for item in service.evaluations.list_suites(tenant_id, limit=500)
        if item["suite_key"] == suite_payload["suite_key"]
    ]
    if matching:
        latest = max(matching, key=lambda item: int(item["version"]))
        suite = service.evaluations.get_suite(tenant_id, latest["id"])
        if suite["status"] == "frozen" and suite["source_ref"] == source_ref:
            return suite
        if suite["status"] != "draft":
            suite = service.evaluations.revise_suite(
                tenant_id,
                suite["id"],
                EvaluationSuiteReviseRequest(
                    expected_record_version=suite["record_version"],
                    source_ref=source_ref,
                ),
                actor,
            )
    else:
        suite = service.evaluations.create_suite(
            tenant_id,
            EvaluationSuiteCreateRequest.model_validate(suite_payload),
            actor,
        )
    cases = [
        EvaluationCaseCreate.model_validate(item)
        for item in definition["evaluation_cases"]
    ]
    suite = service.evaluations.replace_cases(
        tenant_id,
        suite["id"],
        EvaluationCaseReplaceRequest(
            expected_record_version=suite["record_version"], cases=cases
        ),
        actor,
    )
    return service.evaluations.freeze_suite(
        tenant_id,
        suite["id"],
        EvaluationSuiteTransition(
            expected_record_version=suite["record_version"],
            note="M8-R WP4 frozen input and independent oracle",
        ),
        actor,
    )


def shadow_principal(service: Any, tenant_id: str) -> Principal:
    client_id = "m8r-wp4-" + uuid.uuid5(uuid.NAMESPACE_URL, tenant_id).hex
    salt = uuid.uuid5(uuid.NAMESPACE_DNS, client_id).bytes
    with service.db._write_lock, service.db.connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO api_clients(
                id, tenant_id, name, key_salt, key_hash, key_iterations,
                can_supply_order_context, status, created_at, updated_at, role
            ) VALUES (?, ?, 'M8-R shadow workbench', ?, ?, 1, 1, 'active',
                      datetime('now'), datetime('now'), 'client')
            """,
            (client_id, tenant_id, salt, salt),
        )
    return Principal(
        tenant_id=tenant_id,
        client_id=client_id,
        subject_hash="m8r-wp4-shadow-reviewer",
        can_supply_order_context=True,
    )


def run_shadow_case(
    service: Any,
    *,
    tenant_id: str,
    actor: str,
    case_key: str,
    request: ShadowRunRequest,
) -> dict[str, Any]:
    definition = load_m8r_eval_definition()
    case = next((item for item in definition["cases"] if item["case_key"] == case_key), None)
    if case is None:
        raise KeyError("M8-R shadow scenario not found")
    principal = shadow_principal(service, tenant_id)
    session_id = f"m8r-wp4:{case_key}:{request.run_key}"
    responses: list[ChatResponse] = []
    for index, turn in enumerate(case["turns"], start=1):
        responses.append(
            service.chat(
                principal,
                session_id,
                turn["message"],
                turn.get("context", {}),
                idempotency_key=f"m8r-wp4:{case_key}:{request.run_key}:{index}",
                execution_mode="shadow",
                source_type="simulation",
                source_reference=f"{SHADOW_SOURCE_PREFIX}{case_key}",
            )
        )
    service.db.audit(
        "m8r.shadow_scenario.completed",
        actor,
        responses[-1].message_id,
        {
            "case_key": case_key,
            "run_key": request.run_key,
            "turns": len(responses),
            "delivery_statuses": [
                response.suggestion.delivery_status if response.suggestion else None
                for response in responses
            ],
        },
        tenant_id,
    )
    evaluation_case = next(
        item
        for item in definition["evaluation_cases"]
        if item["case_key"] == case_key
    )
    assertion = service.evaluations._evaluate_case(
        {
            **evaluation_case,
            "id": f"shadow:{case_key}",
            "case_hash": _digest(evaluation_case),
        },
        responses,
        None,
    )
    return {
        "contract_version": "m8r-shadow-run-v1",
        "case_key": case_key,
        "run_key": request.run_key,
        "session_id": session_id,
        "input": {"turns": case["turns"]},
        "oracle": case["oracle"],
        "responses": [response.model_dump(mode="json") for response in responses],
        "assertion": assertion,
    }


def run_m8r_eval(
    service: Any, *, tenant_id: str, actor: str, run_key: str
) -> dict[str, Any]:
    suite = ensure_m8r_frozen_suite(service, tenant_id, actor)
    return service.run_evaluation_suite(
        tenant_id,
        suite["id"],
        EvaluationRunRequest(run_key=run_key),
        actor,
        execution_mode="shadow",
    )
