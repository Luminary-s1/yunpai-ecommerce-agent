from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import make_settings
from ecommerce_agent.api import create_app
from ecommerce_agent.evaluation import (
    EvaluationCaseCreate,
    EvaluationCaseReplaceRequest,
    EvaluationError,
    EvaluationExpectation,
    EvaluationRunRequest,
    EvaluationSuiteCreateRequest,
    EvaluationSuiteReviseRequest,
    EvaluationSuiteTransition,
    EvaluationThresholds,
    EvaluationTurn,
)
from ecommerce_agent.releases import (
    ReleaseError,
    ReleasePolicyCreateRequest,
    ReleaseTransitionRequest,
)
from ecommerce_agent.service import AgentService


def _thresholds(**overrides) -> EvaluationThresholds:
    payload = {
        "min_cases": 1,
        "min_pass_rate": 1,
        "min_intent_accuracy": 1,
        "min_handoff_recall": 1,
        "min_evidence_coverage": 1,
        "max_severe_failures": 0,
        "max_regression_rate": 0,
    }
    payload.update(overrides)
    return EvaluationThresholds.model_validate(payload)


@pytest.mark.parametrize(
    "source_type",
    ["virtual", "operational", "mixed", "unknown"],
)
def test_evaluation_expectation_uses_authoritative_source_types(source_type: str) -> None:
    expectation = EvaluationExpectation(expected_source_type=source_type)
    assert expectation.expected_source_type == source_type


def test_evaluation_expectation_rejects_evidence_state_as_source_type() -> None:
    with pytest.raises(ValidationError):
        EvaluationExpectation(expected_source_type="actual")


def _case(
    case_key: str = "case-product",
    *,
    message: str = "尺码怎么选",
    expected_intent: str = "product",
    expected_requires_human: bool = False,
    require_sources: bool = True,
) -> EvaluationCaseCreate:
    return EvaluationCaseCreate(
        case_key=case_key,
        scenario="product",
        source_ref=f"label:{case_key}",
        turns=[
            EvaluationTurn(
                message=message,
                expectation=EvaluationExpectation(
                    expected_intent=expected_intent,
                    expected_requires_human=expected_requires_human,
                    require_sources=require_sources,
                    expected_model_fallback=False,
                ),
            )
        ],
    )


def _suite_request(**overrides) -> EvaluationSuiteCreateRequest:
    payload = {
        "suite_key": "customer-service.regression",
        "name": "客服回归集",
        "description": "去标识化客服标注",
        "source_type": "customer_labeled",
        "source_ref": "customer-export:sha256:test",
        "deidentified": True,
        "required_scenarios": ["product"],
        "thresholds": _thresholds().model_dump(),
    }
    payload.update(overrides)
    return EvaluationSuiteCreateRequest.model_validate(payload)


def _create_frozen(service: AgentService, cases: list[EvaluationCaseCreate] | None = None):
    suite = service.evaluations.create_suite(
        "tenant-test", _suite_request(), "admin-test"
    )
    suite = service.evaluations.replace_cases(
        "tenant-test",
        suite["id"],
        EvaluationCaseReplaceRequest(
            expected_record_version=suite["record_version"],
            cases=cases or [_case()],
        ),
        "admin-test",
    )
    return service.evaluations.freeze_suite(
        "tenant-test",
        suite["id"],
        EvaluationSuiteTransition(expected_record_version=suite["record_version"]),
        "admin-test",
    )


def _response(**overrides):
    payload = {
        "answer": "请以商品详情页的尺码表为准。",
        "intent": "product",
        "risk_level": "low",
        "requires_human": False,
        "sources": [{"id": "knowledge-1"}],
        "model_fallback": False,
        "context_readiness": "ready",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _release_request() -> ReleasePolicyCreateRequest:
    return ReleasePolicyCreateRequest(
        release_key="customer-evaluation.release",
        name="客服评测发布",
        platform="taobao",
        store_id="store-a",
        mode="automatic",
        traffic_percentage=10,
        intent_allowlist=["product"],
        max_risk_level="low",
        require_sources=True,
        allow_model_fallback=False,
        min_replay_cases=1,
        max_replay_failure_rate=0,
        max_replay_severe_errors=0,
        runtime_min_samples=10,
        max_runtime_failure_rate=0.01,
        max_runtime_severe_errors=0,
    )


def test_versioned_suite_redacts_inputs_freezes_and_is_immutable(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        case = _case(message="我的手机号是13812345678，尺码怎么选")
        suite = _create_frozen(service, [case])
        assert suite["status"] == "frozen"
        assert suite["dataset_hash"] and len(suite["dataset_hash"]) == 64
        assert suite["cases"][0]["input_redacted"] is True
        assert "138****5678" in suite["cases"][0]["turns"][0]["message"]
        assert "13812345678" not in str(suite)
        with pytest.raises(EvaluationError, match="only draft"):
            service.evaluations.replace_cases(
                "tenant-test",
                suite["id"],
                EvaluationCaseReplaceRequest(
                    expected_record_version=suite["record_version"], cases=[_case()]
                ),
                "admin-test",
            )
        revised = service.evaluations.revise_suite(
            "tenant-test",
            suite["id"],
            EvaluationSuiteReviseRequest(
                expected_record_version=suite["record_version"],
                source_ref="customer-export:sha256:revision-2",
            ),
            "admin-test",
        )
        assert revised["version"] == 2
        assert revised["status"] == "draft"
        assert revised["previous_suite_id"] == suite["id"]
        assert revised["cases"][0]["case_hash"] == suite["cases"][0]["case_hash"]
    finally:
        service.close()


def test_multi_turn_metrics_baseline_regression_and_failure_details(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        case = EvaluationCaseCreate(
            case_key="multi-turn-product",
            scenario="product",
            source_ref="label:multi",
            turns=[
                EvaluationTurn(message="这款有几个尺码"),
                EvaluationTurn(
                    message="那我应该选哪个",
                    expectation=EvaluationExpectation(
                        expected_intent="product",
                        expected_requires_human=False,
                        require_sources=True,
                        required_answer_terms=["尺码表"],
                        forbidden_answer_terms=["已经退款"],
                        expected_model_fallback=False,
                    ),
                ),
            ],
        )
        suite = _create_frozen(service, [case])
        baseline = service.evaluations.run_suite(
            "tenant-test",
            suite["id"],
            EvaluationRunRequest(run_key="baseline-1"),
            "admin-test",
            lambda item: [_response(), _response()],
        )
        assert baseline["status"] == "passed"
        assert baseline["metrics"]["pass_rate"] == 1
        assert baseline["results"][0]["total_turns"] == 2

        failed = service.evaluations.run_suite(
            "tenant-test",
            suite["id"],
            EvaluationRunRequest(
                run_key="candidate-1", baseline_run_id=baseline["id"]
            ),
            "admin-test",
            lambda item: [
                _response(),
                _response(
                    answer="已经退款",
                    intent="order",
                    requires_human=True,
                    sources=[],
                ),
            ],
        )
        assert failed["status"] == "failed"
        assert failed["metrics"]["regression_rate"] == 1
        assert failed["metrics"]["intent_accuracy"] == 0
        assert failed["metrics"]["evidence_coverage"] == 0
        assert failed["metrics"]["regression_cases"] == ["multi-turn-product"]
        assert failed["results"][0]["severe"] is True
        assert "turn_2:forbidden_answer_term" in failed["results"][0]["violations"]
        assert failed["results"][0]["actual"]["turns"][1]["answer_excerpt"] == "已经退款"
    finally:
        service.close()


def test_run_idempotency_and_tenant_scope(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        suite = _create_frozen(service)

        def execute(_):
            return service.evaluations.run_suite(
                "tenant-test",
                suite["id"],
                EvaluationRunRequest(run_key="concurrent-run"),
                "admin-test",
                lambda item: [_response()],
            )["id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            run_ids = list(pool.map(execute, range(8)))
        assert len(set(run_ids)) == 1
        assert len(service.evaluations.list_runs("tenant-test")) == 1
        with pytest.raises(EvaluationError, match="not found"):
            service.evaluations.get_suite("tenant-other", suite["id"])
        with pytest.raises(EvaluationError, match="different input"):
            service.evaluations.run_suite(
                "tenant-test",
                suite["id"],
                EvaluationRunRequest(
                    run_key="concurrent-run", baseline_run_id=run_ids[0]
                ),
                "admin-test",
                lambda item: [_response()],
            )
    finally:
        service.close()


def test_release_gate_uses_versioned_evaluation_and_rejects_stale_policy(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        suite = _create_frozen(service)
        release = service.releases.create("tenant-test", _release_request(), "creator-a")
        passed = service.evaluations.run_suite(
            "tenant-test",
            suite["id"],
            EvaluationRunRequest(
                run_key="release-pass",
                release_id=release["id"],
                expected_release_record_version=release["record_version"],
            ),
            "reviewer-b",
            lambda item: [_response()],
        )
        assert passed["status"] == "passed"
        assert passed["release_gate_applied"] is True
        evaluated = service.releases.get_policy("tenant-test", release["id"])
        assert evaluated["status"] == "evaluated"
        assert evaluated["evaluation_passed"] is True
        assert evaluated["latest_evaluation_run_id"] == passed["id"]
        assert evaluated["evaluation"]["suite"] == {
            "id": suite["id"],
            "suite_key": suite["suite_key"],
            "version": suite["version"],
            "dataset_hash": suite["dataset_hash"],
        }
        assert evaluated["evaluation"]["gate"]["passed"] is True
        assert evaluated["evaluation"]["metrics"]["pass_rate"] == 1

        stale_release = service.releases.create(
            "tenant-test",
            _release_request().model_copy(update={"release_key": "customer-evaluation.stale"}),
            "creator-a",
        )

        def mutate_release(_):
            with service.db.connect() as conn:
                conn.execute(
                    "UPDATE release_policies SET record_version=record_version+1 "
                    "WHERE id=?",
                    (stale_release["id"],),
                )
            return [_response()]

        stale = service.evaluations.run_suite(
            "tenant-test",
            suite["id"],
            EvaluationRunRequest(
                run_key="release-stale",
                release_id=stale_release["id"],
                expected_release_record_version=stale_release["record_version"],
            ),
            "reviewer-b",
            mutate_release,
        )
        assert stale["status"] == "passed"
        assert stale["release_gate_applied"] is False
        assert "changed while evaluation" in stale["release_gate_error"]
        assert service.releases.get_policy("tenant-test", stale_release["id"])[
            "status"
        ] == "draft"
    finally:
        service.close()


def test_failed_release_evaluation_is_recorded_and_cannot_be_approved(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        suite = _create_frozen(service)
        release = service.releases.create("tenant-test", _release_request(), "creator-a")
        failed = service.evaluations.run_suite(
            "tenant-test",
            suite["id"],
            EvaluationRunRequest(
                run_key="release-failed",
                release_id=release["id"],
                expected_release_record_version=release["record_version"],
            ),
            "reviewer-b",
            lambda item: [_response(intent="order", sources=[])],
        )
        assert failed["status"] == "failed"
        assert failed["release_gate_applied"] is True
        evaluated = service.releases.get_policy("tenant-test", release["id"])
        assert evaluated["status"] == "evaluated"
        assert evaluated["evaluation_passed"] is False
        assert evaluated["evaluation"]["gate"]["passed"] is False
        with pytest.raises(ReleaseError, match="pass replay or versioned evaluation"):
            service.releases.approve(
                "tenant-test",
                release["id"],
                ReleaseTransitionRequest(
                    expected_record_version=evaluated["record_version"]
                ),
                "approver-c",
            )
    finally:
        service.close()


def test_sensitive_output_and_runner_failure_fail_closed_without_raw_data(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        suites = [
            _create_frozen(service),
            service.evaluations.revise_suite(
                "tenant-test",
                service.evaluations.list_suites("tenant-test")[0]["id"],
                EvaluationSuiteReviseRequest(
                    expected_record_version=service.evaluations.list_suites("tenant-test")[0][
                        "record_version"
                    ],
                    source_ref="customer-export:sha256:failure-suite",
                ),
                "admin-test",
            ),
        ]
        revised = service.evaluations.freeze_suite(
            "tenant-test",
            suites[1]["id"],
            EvaluationSuiteTransition(
                expected_record_version=suites[1]["record_version"]
            ),
            "admin-test",
        )
        sensitive = service.evaluations.run_suite(
            "tenant-test",
            suites[0]["id"],
            EvaluationRunRequest(run_key="sensitive-output"),
            "admin-test",
            lambda item: [_response(answer="请联系13812345678")],
        )
        assert sensitive["status"] == "failed"
        assert sensitive["results"][0]["severe"] is True
        assert "turn_1:sensitive_output" in sensitive["results"][0]["violations"]
        assert sensitive["results"][0]["actual"]["turns"][0]["answer_excerpt"] == (
            "请联系138****5678"
        )

        def broken_runner(_item):
            raise TimeoutError("upstream secret must not persist")

        broken = service.evaluations.run_suite(
            "tenant-test",
            revised["id"],
            EvaluationRunRequest(run_key="runner-timeout"),
            "admin-test",
            broken_runner,
        )
        assert broken["status"] == "failed"
        assert broken["results"][0]["violations"] == ["execution_error"]
        assert broken["results"][0]["actual"] == {
            "error_type": "TimeoutError",
            "turns": [],
        }
        database_bytes = service.settings.app_db_path.read_bytes()
        assert b"13812345678" not in database_bytes
        assert b"upstream secret must not persist" not in database_bytes
    finally:
        service.close()


def test_startup_recovers_interrupted_evaluation_once(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    suite = _create_frozen(service)
    run_id = "eval-run-interrupted"
    with service.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO evaluation_runs(
                id, tenant_id, suite_id, run_key, request_hash, status,
                runner_version, dataset_hash, metrics_json, gate_json,
                started_by, created_at
            ) VALUES (?, 'tenant-test', ?, 'interrupted', 'request-hash', 'running',
                      ?, ?, '{}', '{}', 'admin-test', '2026-07-22T00:00:00+00:00')
            """,
            (run_id, suite["id"], service.evaluations.RUNNER_VERSION, suite["dataset_hash"]),
        )
    service.close()

    recovered = AgentService(make_settings(tmp_path))
    try:
        assert recovered.evaluation_recovery == {"recovered": 1, "run_ids": [run_id]}
        run = recovered.evaluations.get_run("tenant-test", run_id)
        assert run["status"] == "error"
        assert run["error_code"] == "interrupted_by_restart"
        with recovered.db.connect() as conn:
            audit_count = conn.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE event_type='evaluation.run_recovered' AND subject_id=?",
                (run_id,),
            ).fetchone()[0]
        assert audit_count == 1
    finally:
        recovered.close()

    restarted = AgentService(make_settings(tmp_path))
    try:
        assert restarted.evaluation_recovery["recovered"] == 0
    finally:
        restarted.close()


def test_actual_agent_evaluation_is_isolated_from_primary_runtime(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        suite = _create_frozen(service)
        with service.db.connect() as conn:
            before = {
                "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "handoffs": conn.execute("SELECT COUNT(*) FROM handoff_tasks").fetchone()[0],
                "outbox": conn.execute("SELECT COUNT(*) FROM channel_outbox").fetchone()[0],
            }
        report = service.run_evaluation_suite(
            "tenant-test",
            suite["id"],
            EvaluationRunRequest(run_key="actual-agent-1"),
            "admin-test",
        )
        assert report["status"] == "passed", report
        assert report["runner_version"] == "customer-agent-eval-v1"
        with service.db.connect() as conn:
            after = {
                "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "handoffs": conn.execute("SELECT COUNT(*) FROM handoff_tasks").fetchone()[0],
                "outbox": conn.execute("SELECT COUNT(*) FROM channel_outbox").fetchone()[0],
            }
        assert after == before
    finally:
        service.close()


def test_evaluation_api_lifecycle_and_error_contracts(tmp_path) -> None:
    app = create_app(make_settings(tmp_path))
    headers = {"X-Admin-Id": "admin-test", "X-Admin-Key": "test-admin-key-123456"}
    with TestClient(app) as client:
        assert client.get("/v1/admin/evaluations/suites").status_code == 401
        created_response = client.post(
            "/v1/admin/evaluations/suites",
            headers=headers,
            json=_suite_request().model_dump(mode="json"),
        )
        assert created_response.status_code == 201, created_response.text
        suite = created_response.json()
        replaced = client.put(
            f"/v1/admin/evaluations/suites/{suite['id']}/cases",
            headers=headers,
            json={
                "expected_record_version": suite["record_version"],
                "cases": [_case().model_dump(mode="json")],
            },
        )
        assert replaced.status_code == 200, replaced.text
        frozen = client.post(
            f"/v1/admin/evaluations/suites/{suite['id']}/freeze",
            headers=headers,
            json={"expected_record_version": replaced.json()["record_version"]},
        )
        assert frozen.status_code == 200, frozen.text
        run = client.post(
            f"/v1/admin/evaluations/suites/{suite['id']}/runs",
            headers=headers,
            json={"run_key": "api-agent-run"},
        )
        assert run.status_code == 201, run.text
        assert run.json()["status"] == "passed"
        assert client.get(
            f"/v1/admin/evaluations/runs/{run.json()['id']}", headers=headers
        ).status_code == 200
        assert client.get(
            "/v1/admin/evaluations/overview", headers=headers
        ).json()["runs"]["passed"] == 1
        stale = client.put(
            f"/v1/admin/evaluations/suites/{suite['id']}/cases",
            headers=headers,
            json={"expected_record_version": 1, "cases": [_case().model_dump(mode="json")]},
        )
        assert stale.status_code == 409
        assert client.get(
            "/v1/admin/evaluations/suites/eval-suite-missing", headers=headers
        ).status_code == 404


def test_evaluation_validation_rejects_unlabeled_and_identified_customer_data() -> None:
    with pytest.raises(ValidationError, match="deidentified"):
        _suite_request(deidentified=False)
    with pytest.raises(ValidationError, match="labeled turn"):
        EvaluationCaseCreate(
            case_key="unlabeled",
            scenario="general",
            turns=[EvaluationTurn(message="你好")],
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EvaluationCaseCreate.model_validate(
            {
                "case_key": "raw-data",
                "scenario": "general",
                "turns": [
                    {
                        "message": "你好",
                        "raw_conversation": "forbidden",
                        "expectation": {"expected_intent": "general"},
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="personal data"):
        _suite_request(source_ref="customer-export:13812345678")
    identified_case = _case().model_dump(mode="json")
    identified_case["source_ref"] = "label:13812345678"
    with pytest.raises(ValidationError, match="personal data"):
        EvaluationCaseCreate.model_validate(identified_case)


def test_frozen_dataset_integrity_detects_case_and_manifest_tampering(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path / "case-tamper"))
    try:
        case_tampered = _create_frozen(service)
        with service.db.connect() as conn:
            conn.execute(
                "UPDATE evaluation_cases SET turns_json='[]' WHERE suite_id=?",
                (case_tampered["id"],),
            )
        with pytest.raises(EvaluationError, match="case integrity"):
            service.evaluations.run_suite(
                "tenant-test",
                case_tampered["id"],
                EvaluationRunRequest(run_key="tampered-case"),
                "admin-test",
                lambda item: [_response()],
            )
    finally:
        service.close()

    service = AgentService(make_settings(tmp_path / "manifest-tamper"))
    try:
        manifest_tampered = _create_frozen(service)
        with service.db.connect() as conn:
            conn.execute(
                "UPDATE evaluation_suites SET dataset_hash=? WHERE id=?",
                ("0" * 64, manifest_tampered["id"]),
            )
        with pytest.raises(EvaluationError, match="dataset integrity"):
            service.evaluations.run_suite(
                "tenant-test",
                manifest_tampered["id"],
                EvaluationRunRequest(run_key="tampered-manifest"),
                "admin-test",
                lambda item: [_response()],
            )
    finally:
        service.close()


def test_baseline_excludes_cases_changed_between_suite_versions(tmp_path) -> None:
    service = AgentService(make_settings(tmp_path))
    try:
        original = _create_frozen(service)
        baseline = service.evaluations.run_suite(
            "tenant-test",
            original["id"],
            EvaluationRunRequest(run_key="baseline-original"),
            "admin-test",
            lambda item: [_response()],
        )
        revised = service.evaluations.revise_suite(
            "tenant-test",
            original["id"],
            EvaluationSuiteReviseRequest(
                expected_record_version=original["record_version"],
                source_ref="customer-export:sha256:changed-case",
            ),
            "admin-test",
        )
        revised = service.evaluations.replace_cases(
            "tenant-test",
            revised["id"],
            EvaluationCaseReplaceRequest(
                expected_record_version=revised["record_version"],
                cases=[_case(message="这款商品的具体尺码如何选择")],
            ),
            "admin-test",
        )
        revised = service.evaluations.freeze_suite(
            "tenant-test",
            revised["id"],
            EvaluationSuiteTransition(
                expected_record_version=revised["record_version"]
            ),
            "admin-test",
        )
        candidate = service.evaluations.run_suite(
            "tenant-test",
            revised["id"],
            EvaluationRunRequest(
                run_key="candidate-changed", baseline_run_id=baseline["id"]
            ),
            "admin-test",
            lambda item: [_response(intent="order", sources=[])],
        )
        assert candidate["metrics"]["comparable_baseline_cases"] == 0
        assert candidate["metrics"]["baseline_changed_cases"] == ["case-product"]
        assert candidate["metrics"]["regression_rate"] == 0
        assert candidate["metrics"]["regression_cases"] == []
    finally:
        service.close()
