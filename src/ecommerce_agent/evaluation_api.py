from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .auth import AdminPrincipal
from .evaluation import (
    EvaluationCaseReplaceRequest,
    EvaluationError,
    EvaluationRunRequest,
    EvaluationSuiteCreateRequest,
    EvaluationSuiteReviseRequest,
    EvaluationSuiteTransition,
)
from .service import AgentService


def build_evaluation_router(
    service: AgentService,
    require_admin: Callable[..., AdminPrincipal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin/evaluations", tags=["customer-evaluations"])

    def conflict(method, admin: AdminPrincipal, *args):
        try:
            return method(admin.tenant_id, *args, admin.admin_id)
        except EvaluationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/mechanism")
    def mechanism_eval(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        """M9-R 机制 Eval 报告（D2：生产入口，纯派生无副作用）。

        返回冻结场景逐场景通过/失败 + 汇总；ground truth 不进入生产输入（硬编码场景）。
        """
        from ecommerce_agent.product_workbench.eval import MechanismEvalRunner

        runner = MechanismEvalRunner()
        results = runner.run_all()
        total = len(results)
        passed = sum(1 for result in results if result.passed)
        return {
            "passed": passed,
            "total": total,
            "all_passed": passed == total,
            "evidence_level": "fixed_table_mock",
            "scenes": [
                {
                    "name": r.scene_name,
                    "passed": r.passed,
                    "failures": r.failures,
                    "produced": r.produced,
                }
                for r in results
            ],
        }

    @router.get("/overview")
    def overview(admin: AdminPrincipal = Depends(require_admin)) -> dict:
        return service.evaluations.overview(admin.tenant_id)

    @router.get("/runs")
    def list_runs(
        suite_id: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        return service.evaluations.list_runs(
            admin.tenant_id, suite_id=suite_id, limit=limit
        )

    @router.get("/runs/{run_id}")
    def get_run(
        run_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.evaluations.get_run(admin.tenant_id, run_id)
        except EvaluationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/suites")
    def list_suites(
        suite_status: str | None = Query(
            default=None, alias="status", pattern=r"^(draft|frozen|retired)$"
        ),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        return service.evaluations.list_suites(
            admin.tenant_id, status=suite_status, limit=limit
        )

    @router.post("/suites", status_code=status.HTTP_201_CREATED)
    def create_suite(
        payload: EvaluationSuiteCreateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return conflict(service.evaluations.create_suite, admin, payload)

    @router.get("/suites/{suite_id}")
    def get_suite(
        suite_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.evaluations.get_suite(admin.tenant_id, suite_id)
        except EvaluationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.put("/suites/{suite_id}/cases")
    def replace_cases(
        suite_id: str,
        payload: EvaluationCaseReplaceRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return conflict(service.evaluations.replace_cases, admin, suite_id, payload)

    @router.post("/suites/{suite_id}/freeze")
    def freeze_suite(
        suite_id: str,
        payload: EvaluationSuiteTransition,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return conflict(service.evaluations.freeze_suite, admin, suite_id, payload)

    @router.post("/suites/{suite_id}/versions", status_code=status.HTTP_201_CREATED)
    def revise_suite(
        suite_id: str,
        payload: EvaluationSuiteReviseRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return conflict(service.evaluations.revise_suite, admin, suite_id, payload)

    @router.post("/suites/{suite_id}/retire")
    def retire_suite(
        suite_id: str,
        payload: EvaluationSuiteTransition,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return conflict(service.evaluations.retire_suite, admin, suite_id, payload)

    @router.post("/suites/{suite_id}/runs", status_code=status.HTTP_201_CREATED)
    def run_suite(
        suite_id: str,
        payload: EvaluationRunRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return conflict(service.run_evaluation_suite, admin, suite_id, payload)

    return router
