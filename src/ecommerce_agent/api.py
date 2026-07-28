from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .auth import AdminPrincipal, AuthError, Principal
from .admin_api import build_admin_router
from .channel_agent import ChannelAgentError
from .config import Settings, is_loopback_host
from .customer_test_api import build_customer_test_router
from .database import SessionScopeError
from .evolution import EvolutionError, EvolutionService
from .evaluation_api import build_evaluation_router
from .handoff import HandoffError
from .handoff_dispatch import DispatchError
from .handoff_staffing import StaffingError
from .governance_api import build_governance_router
from .operations_api import build_operations_router
from .outbox import OutboxReconcileRequest
from .rate_limit import RateLimitError, SlidingWindowRateLimiter
from .release_api import build_release_router
from .simulation_api import build_simulation_router
from .schemas import (
    CandidateView,
    ChatRequest,
    ChatResponse,
    EvolutionDecision,
    FeedbackRequest,
    FeedbackResponse,
    HandoffClaimRequest,
    HandoffEscalateRequest,
    HandoffEventView,
    HandoffNoteRequest,
    HandoffAutoAssignRequest,
    HandoffDispatchAlertAction,
    HandoffDispatchAlertView,
    HandoffDispatchJobView,
    HandoffDispatchRetryRequest,
    HandoffOperatorHeartbeat,
    HandoffOperatorPresenceUpdate,
    HandoffOperatorUpsert,
    HandoffOperatorView,
    HandoffPresenceSessionStart,
    HandoffPresenceSessionView,
    HandoffQueueUpsert,
    HandoffQueueView,
    HandoffRecurringShiftCreate,
    HandoffReassignRequest,
    HandoffShiftCancelRequest,
    HandoffShiftCreate,
    HandoffShiftView,
    HandoffTransition,
    HandoffView,
    RetentionRequest,
)
from .service import AgentService
from .taobao import (
    ChannelReplyRequest,
    OwnershipRequest,
    ReplyDraftCreateRequest,
    ReplyDraftSendRequest,
    ReplyDraftUpdateRequest,
    SubscribeRequest,
    TaobaoError,
    TaobaoRemoteError,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    service = AgentService(settings)
    evolution = EvolutionService(service.db, service.knowledge)
    limiter = SlidingWindowRateLimiter(service.settings.rate_limit_requests_per_minute)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service.start()
        try:
            yield
        finally:
            service.close()

    app = FastAPI(
        title="云湃电商客服 Agent",
        version=__version__,
        description="模块化电商经营、智能客服工作台、竞品洞察、RAG 和受控进化服务",
        lifespan=lifespan,
    )
    app.state.agent = service
    app.state.evolution = evolution
    app.state.rate_limiter = limiter

    architecture_page = Path(__file__).resolve().parents[2] / "docs" / "architecture-inspector.html"

    @app.middleware("http")
    async def enforce_request_size(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > service.settings.max_request_body_bytes:
            return JSONResponse(status_code=413, content={"detail": "request body too large"})
        return await call_next(request)

    def enforce_rate_limit(key: str) -> None:
        try:
            limiter.check(key)
        except RateLimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc

    def require_admin(
        request: Request,
        x_admin_id: str | None = Header(default=None, alias="X-Admin-Id"),
        x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    ) -> AdminPrincipal:
        peer = request.client.host if request.client else "unknown"
        enforce_rate_limit(f"admin:{x_admin_id or peer}")
        if not service.settings.admin_auth_required and not is_loopback_host(peer):
            raise HTTPException(
                status_code=403,
                detail="administrator authentication bypass is limited to loopback clients",
            )
        try:
            return service.auth.authenticate_admin(x_admin_id, x_admin_key)
        except AuthError as exc:
            status = 503 if not service.auth.admin_configured else 401
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    def require_client(
        request: Request,
        x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
        x_client_key: str | None = Header(default=None, alias="X-Client-Key"),
        x_subject_id: str | None = Header(default=None, alias="X-Subject-Id"),
    ) -> Principal:
        peer = request.client.host if request.client else "unknown"
        enforce_rate_limit(f"client:{x_client_id or peer}")
        try:
            return service.auth.authenticate(x_client_id, x_client_key, x_subject_id)
        except AuthError as exc:
            status = 503 if not service.auth.configured else 401
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    def require_local_customer_test(request: Request) -> Principal:
        peer = request.client.host if request.client else "unknown"
        enforce_rate_limit(f"customer-test:{peer}")
        if not is_loopback_host(peer):
            raise HTTPException(
                status_code=403,
                detail="customer test interface is limited to loopback clients",
            )
        if not service.settings.customer_test_enabled:
            raise HTTPException(status_code=404, detail="customer test interface is disabled")
        try:
            return service.auth.authenticate(
                service.settings.bootstrap_client_id,
                service.settings.bootstrap_client_key,
                "local-customer-test",
            )
        except AuthError as exc:
            raise HTTPException(
                status_code=503,
                detail="customer test interface requires a configured bootstrap client",
            ) from exc

    app.include_router(build_operations_router(service, require_admin))
    app.include_router(build_admin_router(service, require_admin))
    app.include_router(build_governance_router(service, require_admin))
    app.include_router(build_release_router(service, require_admin))
    app.include_router(build_evaluation_router(service, require_admin))
    app.include_router(build_simulation_router(service, require_admin))
    app.include_router(build_customer_test_router(service, require_local_customer_test))

    @app.get("/health")
    def health() -> dict:
        return service.health()

    @app.get("/ready")
    def ready() -> JSONResponse:
        is_ready, detail = service.readiness()
        return JSONResponse(status_code=200 if is_ready else 503, content=detail)

    @app.get("/architecture", include_in_schema=False)
    def architecture() -> FileResponse:
        if not architecture_page.is_file():
            raise HTTPException(status_code=404, detail="architecture inspector is not built")
        return FileResponse(architecture_page, media_type="text/html; charset=utf-8")

    pressure_report_page = (
        Path(__file__).resolve().parents[2] / "docs" / "marketing-finance-pressure-report.html"
    )
    pressure_report_data = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "marketing-finance-pressure-evidence.json"
    )

    @app.get("/reports/marketing-finance-pressure", include_in_schema=False)
    def marketing_finance_pressure_report() -> FileResponse:
        if not pressure_report_page.is_file():
            raise HTTPException(status_code=404, detail="marketing-finance pressure report is not built")
        return FileResponse(pressure_report_page, media_type="text/html; charset=utf-8")

    @app.get("/reports/marketing-finance-pressure.json", include_in_schema=False)
    def marketing_finance_pressure_evidence() -> FileResponse:
        if not pressure_report_data.is_file():
            raise HTTPException(status_code=404, detail="marketing-finance pressure evidence is not built")
        return FileResponse(pressure_report_data, media_type="application/json; charset=utf-8")

    admin_console_page = Path(__file__).resolve().parents[2] / "docs" / "admin-console.html"

    @app.get("/admin", include_in_schema=False)
    def admin_console() -> FileResponse:
        if not admin_console_page.is_file():
            raise HTTPException(status_code=404, detail="admin console is not built")
        return FileResponse(admin_console_page, media_type="text/html; charset=utf-8")

    customer_test_page = Path(__file__).resolve().parents[2] / "docs" / "customer-test.html"

    @app.get("/customer-test", include_in_schema=False)
    def customer_test(request: Request) -> FileResponse:
        require_local_customer_test(request)
        if not customer_test_page.is_file():
            raise HTTPException(status_code=404, detail="customer test page is not built")
        return FileResponse(customer_test_page, media_type="text/html; charset=utf-8")

    @app.get("/v1/channels/adapters")
    def channel_adapter_catalog(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        return [item.model_dump() for item in service.channel_adapters.catalog()]

    @app.get("/v1/integrations/taobao/capabilities")
    def taobao_capabilities(admin: AdminPrincipal = Depends(require_admin)) -> dict:
        return service.taobao.capabilities(admin.tenant_id)

    @app.get("/v1/integrations/taobao/outbox/summary")
    def taobao_outbox_summary(admin: AdminPrincipal = Depends(require_admin)) -> dict:
        return service.taobao.outbox_summary(admin.tenant_id)

    @app.get("/v1/integrations/taobao/outbox")
    def taobao_outbox_list(
        status: str | None = Query(default=None, pattern=r"^(queued|sending|sent|failed)$"),
        delivery_state: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        return service.taobao.list_outbox(
            admin.tenant_id,
            status=status,
            delivery_state=delivery_state,
            limit=limit,
        )

    @app.post("/v1/integrations/taobao/outbox/run")
    def taobao_outbox_run(
        limit: int = Query(default=20, ge=1, le=100),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return service.taobao.run_outbox_once(
            worker_id=f"admin-{admin.admin_id}", limit=limit
        )

    @app.post("/v1/integrations/taobao/outbox/{outbox_id}/reconcile")
    def taobao_outbox_reconcile(
        outbox_id: str,
        payload: OutboxReconcileRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.taobao.reconcile_outbox(
                outbox_id, admin.tenant_id, payload, admin.admin_id
            )
        except TaobaoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/integrations/taobao/agent-jobs/summary")
    def taobao_agent_job_summary(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return service.channel_agents.summary(admin.tenant_id)

    @app.get("/v1/integrations/taobao/agent-jobs")
    def taobao_agent_jobs(
        status: str | None = Query(
            default=None,
            pattern=r"^(queued|running|retry|completed|blocked|dead_letter)$",
        ),
        conversation_id: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=100, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        return service.channel_agents.list_jobs(
            admin.tenant_id,
            status=status,
            conversation_id=conversation_id,
            limit=limit,
        )

    @app.get("/v1/integrations/taobao/agent-jobs/{job_id}")
    def taobao_agent_job(
        job_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.channel_agents.get_job(job_id, admin.tenant_id)
        except ChannelAgentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/integrations/taobao/agent-jobs/run")
    def taobao_agent_jobs_run(
        limit: int = Query(default=10, ge=1, le=100),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return service.channel_agents.run_once(
            worker_id=f"admin-{admin.admin_id}", limit=limit
        )

    @app.post("/v1/integrations/taobao/authorize")
    def taobao_authorize(
        shop_id: str = Query(min_length=1, max_length=128),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.taobao.begin_authorization(admin.tenant_id, shop_id)
        except TaobaoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/integrations/taobao/oauth/callback")
    def taobao_oauth_callback(
        code: str = Query(min_length=1), state: str = Query(min_length=16)
    ) -> dict:
        try:
            return service.taobao.complete_authorization(code, state)
        except TaobaoError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TaobaoRemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    def process_taobao_bot_message(inbound) -> None:
        if inbound.is_new and inbound.job_id:
            service.channel_agents.run_job_once(inbound.job_id)

    @app.post("/v1/integrations/taobao/qimen")
    async def taobao_qimen(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
        peer = request.client.host if request.client else "unknown"
        enforce_rate_limit(f"taobao-qimen:{peer}")
        raw_body = await request.body()
        try:
            params = dict(parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True))
            inbound = service.taobao.receive_qimen(params)
        except (UnicodeDecodeError, TaobaoError) as exc:
            service.db.audit(
                "taobao.qimen.rejected",
                "qimen",
                None,
                {"reason": str(exc)[:300]},
                service.settings.bootstrap_tenant_id,
            )
            return JSONResponse(
                status_code=400,
                content={"result": {"success": False, "resultCode": "INVALID_REQUEST", "errorMessage": str(exc)}},
            )
        background_tasks.add_task(process_taobao_bot_message, inbound)
        return JSONResponse(
            content={"result": {"success": True, "resultCode": "SUCCESS", "errorMessage": ""}}
        )

    @app.get("/v1/integrations/taobao/conversations")
    def taobao_conversations(
        owner_mode: str | None = Query(default=None, pattern=r"^(bot|human|paused)$"),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[dict]:
        return service.taobao.list_conversations(admin.tenant_id, owner_mode)

    @app.get("/v1/integrations/taobao/conversations/{conversation_id}")
    def taobao_conversation_detail(
        conversation_id: str,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.taobao.conversation_detail(conversation_id, admin.tenant_id)
        except TaobaoError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/integrations/taobao/conversations/{conversation_id}/ownership")
    def taobao_change_ownership(
        conversation_id: str,
        payload: OwnershipRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.taobao.change_ownership(
                conversation_id, admin.tenant_id, payload, admin.admin_id
            )
        except TaobaoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/integrations/taobao/conversations/{conversation_id}/messages")
    def taobao_send_message(
        conversation_id: str,
        payload: ChannelReplyRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.taobao.send_reply(
                conversation_id, admin.tenant_id, payload, admin.admin_id
            )
        except TaobaoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TaobaoRemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post(
        "/v1/integrations/taobao/conversations/{conversation_id}/reply-drafts",
        status_code=201,
    )
    def taobao_create_reply_draft(
        conversation_id: str,
        payload: ReplyDraftCreateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.taobao.create_reply_draft(
                conversation_id, admin.tenant_id, payload, admin.admin_id
            )
        except TaobaoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.patch(
        "/v1/integrations/taobao/conversations/{conversation_id}/reply-drafts/{draft_id}"
    )
    def taobao_update_reply_draft(
        conversation_id: str,
        draft_id: str,
        payload: ReplyDraftUpdateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.taobao.update_reply_draft(
                conversation_id, draft_id, admin.tenant_id, payload, admin.admin_id
            )
        except TaobaoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/integrations/taobao/conversations/{conversation_id}/reply-drafts/{draft_id}/send"
    )
    def taobao_send_reply_draft(
        conversation_id: str,
        draft_id: str,
        payload: ReplyDraftSendRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.taobao.send_reply_draft(
                conversation_id, draft_id, admin.tenant_id, payload, admin.admin_id
            )
        except TaobaoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TaobaoRemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/integrations/taobao/chatrobot/subscription")
    def taobao_subscription(
        payload: SubscribeRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.taobao.subscribe(admin.tenant_id, payload, admin.admin_id)
        except TaobaoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TaobaoRemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/v1/integrations/taobao/chatrobot/subscription")
    def taobao_subscription_status(
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.taobao.subscription_status(admin.tenant_id)
        except TaobaoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except TaobaoRemoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/v1/chat", response_model=ChatResponse)
    def chat(payload: ChatRequest, principal: Principal = Depends(require_client)) -> ChatResponse:
        try:
            return service.chat(principal, payload.session_id, payload.message, payload.context)
        except SessionScopeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/feedback", response_model=FeedbackResponse)
    def feedback(
        payload: FeedbackRequest, principal: Principal = Depends(require_client)
    ) -> FeedbackResponse:
        try:
            trusted_payload = payload.model_copy(update={"submitted_by": principal.client_id})
            return evolution.submit_feedback(trusted_payload, tenant_id=principal.tenant_id)
        except EvolutionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/handoffs/summary")
    def handoff_summary(
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return service.handoffs.summary(tenant_id=admin.tenant_id, scope=scope)

    @app.get("/v1/handoffs/queues", response_model=list[HandoffQueueView])
    def list_handoff_queues(
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[HandoffQueueView]:
        return service.handoffs.list_queues(tenant_id=admin.tenant_id, scope=scope)

    @app.put("/v1/handoffs/queues/{queue_key}", response_model=HandoffQueueView)
    def upsert_handoff_queue(
        queue_key: str,
        payload: HandoffQueueUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffQueueView:
        if queue_key != payload.queue_key:
            raise HTTPException(status_code=422, detail="queue key path and payload differ")
        try:
            return service.handoffs.upsert_queue(
                tenant_id=admin.tenant_id, value=payload, actor=admin.admin_id
            )
        except HandoffError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/handoffs/escalate-due")
    def escalate_due_handoffs(
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        report = service.handoffs.escalate_due(
            tenant_id=admin.tenant_id, scope=scope
        )
        service.db.audit(
            "handoff.sla_scan_requested",
            admin.admin_id,
            admin.tenant_id,
            report,
            admin.tenant_id,
        )
        return report

    @app.get("/v1/handoffs/operators", response_model=list[HandoffOperatorView])
    def list_handoff_operators(
        status: str | None = Query(default=None),
        presence: str | None = Query(default=None),
        queue_key: str | None = Query(default=None),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[HandoffOperatorView]:
        try:
            return service.handoff_staffing.list(
                tenant_id=admin.tenant_id,
                status=status,
                presence=presence,
                queue_key=queue_key,
            )
        except StaffingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get(
        "/v1/handoffs/operators/{operator_id}", response_model=HandoffOperatorView
    )
    def get_handoff_operator(
        operator_id: str, admin: AdminPrincipal = Depends(require_admin)
    ) -> HandoffOperatorView:
        saved = service.handoff_staffing.get(
            tenant_id=admin.tenant_id, operator_id=operator_id
        )
        if saved is None:
            raise HTTPException(status_code=404, detail="operator profile not found")
        return saved

    @app.put(
        "/v1/handoffs/operators/{operator_id}", response_model=HandoffOperatorView
    )
    def upsert_handoff_operator(
        operator_id: str,
        payload: HandoffOperatorUpsert,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffOperatorView:
        try:
            return service.handoff_staffing.upsert(
                tenant_id=admin.tenant_id,
                operator_id=operator_id,
                value=payload,
                actor=admin.admin_id,
            )
        except StaffingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/handoffs/operators/{operator_id}/presence",
        response_model=HandoffOperatorView,
    )
    def update_handoff_operator_presence(
        operator_id: str,
        payload: HandoffOperatorPresenceUpdate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffOperatorView:
        if operator_id != admin.admin_id:
            raise HTTPException(
                status_code=403, detail="operators may only update their own presence"
            )
        try:
            return service.handoff_staffing.update_presence(
                tenant_id=admin.tenant_id,
                operator_id=operator_id,
                value=payload,
                actor=admin.admin_id,
            )
        except StaffingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/handoffs/operators/{operator_id}/presence-sessions",
        response_model=HandoffPresenceSessionView,
    )
    def start_handoff_operator_presence_session(
        operator_id: str,
        payload: HandoffPresenceSessionStart,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffPresenceSessionView:
        if operator_id != admin.admin_id:
            raise HTTPException(
                status_code=403, detail="operators may only start their own presence session"
            )
        try:
            return service.handoff_staffing.start_presence_session(
                tenant_id=admin.tenant_id,
                operator_id=operator_id,
                value=payload,
                actor=admin.admin_id,
            )
        except StaffingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/handoffs/operators/{operator_id}/heartbeat",
        response_model=HandoffPresenceSessionView,
    )
    def heartbeat_handoff_operator(
        operator_id: str,
        payload: HandoffOperatorHeartbeat,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffPresenceSessionView:
        if operator_id != admin.admin_id:
            raise HTTPException(
                status_code=403, detail="operators may only heartbeat their own presence"
            )
        try:
            return service.handoff_staffing.heartbeat(
                tenant_id=admin.tenant_id,
                operator_id=operator_id,
                value=payload,
                actor=admin.admin_id,
            )
        except StaffingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/v1/handoffs/operators/{operator_id}/shifts",
        response_model=list[HandoffShiftView],
    )
    def list_handoff_operator_shifts(
        operator_id: str,
        from_at: datetime | None = Query(default=None),
        to_at: datetime | None = Query(default=None),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[HandoffShiftView]:
        if (from_at is not None and from_at.tzinfo is None) or (
            to_at is not None and to_at.tzinfo is None
        ):
            raise HTTPException(status_code=422, detail="shift filters require a UTC offset")
        return service.handoff_staffing.list_shifts(
            tenant_id=admin.tenant_id,
            operator_id=operator_id,
            from_at=from_at,
            to_at=to_at,
        )

    @app.post(
        "/v1/handoffs/operators/{operator_id}/shifts",
        response_model=HandoffShiftView,
        status_code=201,
    )
    def create_handoff_operator_shift(
        operator_id: str,
        payload: HandoffShiftCreate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffShiftView:
        try:
            return service.handoff_staffing.create_shift(
                tenant_id=admin.tenant_id,
                operator_id=operator_id,
                value=payload,
                actor=admin.admin_id,
            )
        except StaffingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/handoffs/operators/{operator_id}/shifts/recurring",
        response_model=list[HandoffShiftView],
        status_code=201,
    )
    def create_handoff_operator_recurring_shifts(
        operator_id: str,
        payload: HandoffRecurringShiftCreate,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[HandoffShiftView]:
        try:
            return service.handoff_staffing.create_recurring_shifts(
                tenant_id=admin.tenant_id,
                operator_id=operator_id,
                value=payload,
                actor=admin.admin_id,
            )
        except StaffingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/handoffs/operators/{operator_id}/shifts/{shift_id}/cancel",
        response_model=HandoffShiftView,
    )
    def cancel_handoff_operator_shift(
        operator_id: str,
        shift_id: str,
        payload: HandoffShiftCancelRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffShiftView:
        try:
            return service.handoff_staffing.cancel_shift(
                tenant_id=admin.tenant_id,
                operator_id=operator_id,
                shift_id=shift_id,
                value=payload,
                actor=admin.admin_id,
            )
        except StaffingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/handoffs/dispatch/summary")
    def handoff_dispatch_summary(
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        return {
            **service.handoff_dispatch.summary(
                tenant_id=admin.tenant_id, scope=scope
            ),
            "worker": service.handoff_dispatch_worker_status(),
        }

    @app.get(
        "/v1/handoffs/dispatch/jobs", response_model=list[HandoffDispatchJobView]
    )
    def list_handoff_dispatch_jobs(
        status: str | None = Query(default=None),
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        limit: int = Query(default=200, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[HandoffDispatchJobView]:
        try:
            return service.handoff_dispatch.list_jobs(
                tenant_id=admin.tenant_id,
                status=status,
                scope=scope,
                limit=limit,
            )
        except DispatchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get(
        "/v1/handoffs/dispatch/alerts",
        response_model=list[HandoffDispatchAlertView],
    )
    def list_handoff_dispatch_alerts(
        status: str | None = Query(default=None),
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        limit: int = Query(default=200, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[HandoffDispatchAlertView]:
        try:
            return service.handoff_dispatch.list_alerts(
                tenant_id=admin.tenant_id,
                status=status,
                scope=scope,
                limit=limit,
            )
        except DispatchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/handoffs/dispatch/run")
    def run_handoff_dispatch(
        limit: int = Query(default=20, ge=1, le=100),
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            return service.handoff_dispatch.run_once(
                worker_id=f"admin-{admin.admin_id}",
                limit=limit,
                tenant_id=admin.tenant_id,
                scope=scope,
            )
        except DispatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/handoffs/dispatch/jobs/{job_id}/retry",
        response_model=HandoffDispatchJobView,
    )
    def retry_handoff_dispatch_job(
        job_id: str,
        payload: HandoffDispatchRetryRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffDispatchJobView:
        try:
            return service.handoff_dispatch.retry_job(
                tenant_id=admin.tenant_id,
                job_id=job_id,
                value=payload,
                actor=admin.admin_id,
            )
        except DispatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/handoffs/dispatch/alerts/{alert_id}/acknowledge",
        response_model=HandoffDispatchAlertView,
    )
    def acknowledge_handoff_dispatch_alert(
        alert_id: str,
        payload: HandoffDispatchAlertAction,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffDispatchAlertView:
        try:
            return service.handoff_dispatch.acknowledge_alert(
                tenant_id=admin.tenant_id,
                alert_id=alert_id,
                value=payload,
                actor=admin.admin_id,
            )
        except DispatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/handoffs", response_model=list[HandoffView])
    def list_handoffs(
        status: str | None = Query(default=None),
        queue_key: str | None = Query(default=None),
        priority: str | None = Query(default=None),
        assigned_to: str | None = Query(default=None),
        sla: str | None = Query(default=None),
        scope: str = Query(
            default="operational",
            pattern=r"^(operational|simulation|evaluation|all)$",
        ),
        limit: int = Query(default=200, ge=1, le=500),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[HandoffView]:
        try:
            return service.handoffs.list(
                tenant_id=admin.tenant_id,
                status=status,
                queue_key=queue_key,
                priority=priority,
                assigned_to=assigned_to,
                sla=sla,
                scope=scope,
                limit=limit,
            )
        except HandoffError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/handoffs/{handoff_id}", response_model=HandoffView)
    def get_handoff(
        handoff_id: str, admin: AdminPrincipal = Depends(require_admin)
    ) -> HandoffView:
        try:
            return service.handoffs.get(
                tenant_id=admin.tenant_id, handoff_id=handoff_id
            )
        except HandoffError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get(
        "/v1/handoffs/{handoff_id}/history", response_model=list[HandoffEventView]
    )
    def handoff_history(
        handoff_id: str, admin: AdminPrincipal = Depends(require_admin)
    ) -> list[HandoffEventView]:
        try:
            return service.handoffs.history(
                tenant_id=admin.tenant_id, handoff_id=handoff_id
            )
        except HandoffError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/handoffs/{handoff_id}/claim", response_model=HandoffView)
    def claim_handoff(
        handoff_id: str,
        payload: HandoffClaimRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffView:
        try:
            return service.handoffs.claim(
                tenant_id=admin.tenant_id,
                handoff_id=handoff_id,
                operator=admin.admin_id,
                expected_version=payload.expected_version,
                note=payload.note,
            )
        except HandoffError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/handoffs/{handoff_id}/assign-best", response_model=HandoffView)
    def auto_assign_handoff(
        handoff_id: str,
        payload: HandoffAutoAssignRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffView:
        try:
            return service.handoffs.auto_assign(
                tenant_id=admin.tenant_id,
                handoff_id=handoff_id,
                expected_version=payload.expected_version,
                actor=admin.admin_id,
                note=payload.note,
            )
        except HandoffError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/handoffs/{handoff_id}/transition", response_model=HandoffView)
    def transition_handoff(
        handoff_id: str,
        payload: HandoffTransition,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffView:
        try:
            return service.handoffs.transition(
                tenant_id=admin.tenant_id,
                handoff_id=handoff_id,
                target_status=payload.target_status,
                operator=admin.admin_id,
                expected_version=payload.expected_version,
                note=payload.note,
            )
        except HandoffError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/handoffs/{handoff_id}/reassign", response_model=HandoffView)
    def reassign_handoff(
        handoff_id: str,
        payload: HandoffReassignRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffView:
        try:
            return service.handoffs.reassign(
                tenant_id=admin.tenant_id,
                handoff_id=handoff_id,
                assigned_to=payload.assigned_to,
                expected_version=payload.expected_version,
                actor=admin.admin_id,
                note=payload.note,
                queue_key=payload.queue_key,
            )
        except HandoffError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/handoffs/{handoff_id}/escalate", response_model=HandoffView)
    def escalate_handoff(
        handoff_id: str,
        payload: HandoffEscalateRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffView:
        try:
            return service.handoffs.escalate(
                tenant_id=admin.tenant_id,
                handoff_id=handoff_id,
                expected_version=payload.expected_version,
                actor=admin.admin_id,
                note=payload.note,
                queue_key=payload.queue_key,
            )
        except HandoffError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/handoffs/{handoff_id}/notes", response_model=HandoffView)
    def add_handoff_note(
        handoff_id: str,
        payload: HandoffNoteRequest,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> HandoffView:
        try:
            return service.handoffs.add_note(
                tenant_id=admin.tenant_id,
                handoff_id=handoff_id,
                expected_version=payload.expected_version,
                actor=admin.admin_id,
                note=payload.note,
            )
        except HandoffError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/metrics/summary")
    def metrics_summary(admin: AdminPrincipal = Depends(require_admin)) -> dict:
        return service.db.metric_summary(admin.tenant_id)

    @app.post("/v1/maintenance/retention")
    def run_retention(
        payload: RetentionRequest, admin: AdminPrincipal = Depends(require_admin)
    ) -> dict:
        return service.purge_expired(actor=admin.admin_id, dry_run=payload.dry_run)

    @app.get("/v1/evolution/candidates", response_model=list[CandidateView])
    def list_candidates(
        status: str | None = Query(default=None),
        admin: AdminPrincipal = Depends(require_admin),
    ) -> list[CandidateView]:
        return evolution.list_candidates(status, tenant_id=admin.tenant_id)

    @app.post("/v1/evolution/candidates/{candidate_id}/evaluate", response_model=CandidateView)
    def evaluate(
        candidate_id: str, admin: AdminPrincipal = Depends(require_admin)
    ) -> CandidateView:
        try:
            return evolution.evaluate(candidate_id, tenant_id=admin.tenant_id)
        except EvolutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/evolution/candidates/{candidate_id}/approve", response_model=CandidateView)
    def approve(
        candidate_id: str,
        payload: EvolutionDecision,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> CandidateView:
        try:
            return evolution.approve(
                candidate_id, admin.admin_id, payload.note, tenant_id=admin.tenant_id
            )
        except EvolutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/evolution/candidates/{candidate_id}/reject", response_model=CandidateView)
    def reject(
        candidate_id: str,
        payload: EvolutionDecision,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> CandidateView:
        try:
            return evolution.reject(
                candidate_id, admin.admin_id, payload.note, tenant_id=admin.tenant_id
            )
        except EvolutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/evolution/knowledge/{knowledge_id}/rollback")
    def rollback(
        knowledge_id: str,
        payload: EvolutionDecision,
        admin: AdminPrincipal = Depends(require_admin),
    ) -> dict:
        try:
            changed = evolution.rollback(
                knowledge_id, admin.admin_id, payload.note, tenant_id=admin.tenant_id
            )
            return {"knowledge_id": knowledge_id, "retired": changed}
        except EvolutionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app
