from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver

from .auth import AuthenticationService, Principal
from .admin import AdminConsoleService
from .business import OperationsService
from .channel_agent import ChannelAgentRuntime
from .channel_sdk import ChannelAdapterRegistry
from .channel_sdk.mockchat import MockChatChannelAdapter
from .channel_sdk.taobao_adapter import TaobaoChannelAdapter
from .config import Settings
from .context_builder import ContextBuilder
from .database import Database, SessionScopeError, utc_now
from .disaster_recovery import DataDirectoryLock
from .evaluation import EvaluationRunRequest, EvaluationService
from .graph import MODEL_UNAVAILABLE_HANDOFF_ANSWER, build_graph, verify_response
from .handoff import HandoffService
from .handoff_dispatch import HandoffDispatchService
from .handoff_staffing import HandoffStaffingService
from .knowledge_management import KnowledgeManagementService
from .knowledge_seed import seed_records
from .llm import ModelGateway
from .maintenance import MaintenanceService
from .policy import sanitize_context
from .prompts import SYSTEM_PROMPT, build_messages
from .rag import KnowledgeBase
from .quality import QualityService
from .releases import ReleaseReplayRequest, ReleaseService
from .readonly_data import ReadonlyDataService, ReadonlyReportIngestionService
from .readonly_readiness import ReadonlyDemoService, ReadonlyReadinessService
from .product_identity import ProductIdentityService
from .schemas import ChatResponse, SourceItem
from .text_utils import normalize_text, redact_sensitive
from .taobao import TaobaoIntegrationService
from .sops import SopService
from .tokens import count_tokens
from .tools import ToolRegistry
from .traffic_lab import TrafficAnalysisModelInterpreter
from .product_lifecycle.engine import RecommendationModelInterpreter
from .product_diagnosis.interpreter import DiagnosisModelInterpreter


logger = logging.getLogger("ecommerce_agent.service")

# 进程级缓存：同一进程内只导入一次知识资产（避免每次启动重复导入拖慢）
# 数据库是新临时目录时仍需导入（key 用 db 路径区分）
_kg_import_cache: dict[str, dict[str, int]] = {}


class AgentService:
    SESSION_IDLE_WORKER_POLL_SECONDS = 60.0

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        tool_registry: ToolRegistry | None = None,
    ):
        self.settings = settings or Settings.from_env()
        self.settings.ensure_directories()
        self._runtime_lock = DataDirectoryLock(self.settings.data_dir)
        self._runtime_lock.acquire()
        try:
            self.db = Database(self.settings.app_db_path)
            self.db.initialize()
            self.contexts = ContextBuilder(self.db)
            self.auth = AuthenticationService(self.db, self.settings)
            self.admin = AdminConsoleService(self.db, self.contexts)
            self.knowledge = KnowledgeBase(self.db)
            self.knowledge_management = KnowledgeManagementService(self.db, self.knowledge)
            # P1-2 接入：店铺级长期记忆服务（此前仅有定义，生产零调用——A1 修复）。
            # 延迟导入避免触发 knowledge_engine 包 __init__ → graph_api → service 循环导入。
            from .knowledge_engine.memory_service import KnowledgeMemoryService

            self.memory = KnowledgeMemoryService(self.knowledge)
            self.seeded_count = self.knowledge.seed_if_empty(seed_records())
            # M3 知识库接入：启动时把 02_clean 资产层导入运行时表（幂等）。
            # 深水区1修复：此前 knowledge_engine 是死代码，客服回答只用内置种子；
            # 现在 kg-* 知识进入 RAG 检索，222 节点知识图谱真正服务客服。
            self.kg_import_stats = self._import_knowledge_assets()
            self.model = ModelGateway(self.settings)
            self.handoff_staffing = HandoffStaffingService(self.db)
            self.handoffs = HandoffService(self.db, self.handoff_staffing)
            self.handoffs.ensure_default_queues(self.settings.bootstrap_tenant_id)
            if self.settings.admin_api_key:
                self.handoff_staffing.ensure_bootstrap_operator(
                    tenant_id=self.settings.bootstrap_tenant_id,
                    operator_id=self.settings.bootstrap_admin_id,
                    display_name="Bootstrap appliance administrator",
                )
            self.handoff_dispatch = HandoffDispatchService(
                self.db,
                self.handoffs,
                self.handoff_staffing,
                lease_seconds=self.settings.handoff_dispatch_lease_seconds,
                max_attempts=self.settings.handoff_dispatch_max_attempts,
                retry_base_seconds=self.settings.handoff_dispatch_retry_base_seconds,
                retry_max_seconds=self.settings.handoff_dispatch_retry_max_seconds,
            )
            self.handoffs.set_dispatcher(self.handoff_dispatch)
            self.handoff_staffing.set_dispatch_waker(
                self.handoff_dispatch.wake_for_operator
            )
            self.handoff_dispatch_recovery = self.handoff_dispatch.ensure_pending_jobs()
            self.tools = tool_registry or ToolRegistry()
            traffic_analysis_interpreter = (
                TrafficAnalysisModelInterpreter(self.model)
                if self.settings.model_enabled
                else None
            )
            # D-034：M9-R 诊断→建议语义链接模型（模型可用时走模型，否则 Ruleset 降级）。
            recommendation_interpreter = (
                RecommendationModelInterpreter(self.model)
                if self.settings.model_enabled
                else None
            )
            diagnosis_interpreter = (
                DiagnosisModelInterpreter(self.model)
                if self.settings.model_enabled
                else None
            )
            self.operations = OperationsService(
                self.db,
                traffic_analysis_interpreter=traffic_analysis_interpreter,
                recommendation_interpreter=recommendation_interpreter,
                diagnosis_interpreter=diagnosis_interpreter,
            )
            self.operations.register_agent_tools(self.tools)
            self.readonly_data = ReadonlyDataService(self.db)
            self.readonly_ingestion = ReadonlyReportIngestionService(self.db)
            self.product_identity = ProductIdentityService(self.db)
            self.readonly_readiness = ReadonlyReadinessService(self.db)
            self.readonly_demo = ReadonlyDemoService(self.db)
            if self.settings.model_enabled:
                # 文案与报告解读可走真实模型；模型异常时服务内部自动降级到模板。
                self.operations.ops_assistant.attach_model(self.model)
            self._competitive_worker_thread: threading.Thread | None = None
            self._competitive_worker_stop = threading.Event()
            self._competitive_worker_lock = threading.Lock()
            self._competitive_worker_last_error: str | None = None
            self._competitive_worker_last_run_at: str | None = None
            self._competitive_worker_cycles = 0
            self._competitive_worker_evaluated = 0
            self._handoff_sla_worker_thread: threading.Thread | None = None
            self._handoff_sla_worker_stop = threading.Event()
            self._handoff_sla_worker_lock = threading.Lock()
            self._handoff_sla_worker_last_error: str | None = None
            self._handoff_sla_worker_last_run_at: str | None = None
            self._handoff_sla_worker_cycles = 0
            self._handoff_sla_worker_escalated = 0
            self._handoff_dispatch_worker_thread: threading.Thread | None = None
            self._handoff_dispatch_worker_stop = threading.Event()
            self._handoff_dispatch_worker_lock = threading.Lock()
            self._handoff_dispatch_worker_last_error: str | None = None
            self._handoff_dispatch_worker_last_run_at: str | None = None
            self._handoff_dispatch_worker_cycles = 0
            self._handoff_dispatch_worker_assigned = 0
            self._handoff_dispatch_worker_waiting = 0
            self._handoff_dispatch_worker_failed = 0
            self._session_idle_worker_thread: threading.Thread | None = None
            self._session_idle_worker_stop = threading.Event()
            self._session_idle_worker_lock = threading.Lock()
            self._session_idle_worker_last_error: str | None = None
            self._session_idle_worker_last_run_at: str | None = None
            self._session_idle_worker_cycles = 0
            self._session_idle_worker_closed = 0
            # 知识库自维护 worker（A3：梦循环 + 每日评测接入服务生命周期）
            self._knowledge_worker_thread: threading.Thread | None = None
            self._knowledge_worker_stop = threading.Event()
            self._knowledge_worker_lock = threading.Lock()
            self._knowledge_worker_last_error: str | None = None
            self._knowledge_worker_last_run_at: str | None = None
            self._knowledge_worker_cycles = 0
            self._knowledge_worker_last_report: dict[str, Any] = {}
            self.sops = SopService(self.db, self.tools)
            self.seeded_sops = self.sops.seed_defaults(self.settings.bootstrap_tenant_id)
            self.sop_recovery = self.sops.recover_interrupted_runs()
            self.quality = QualityService(self.db)
            self.releases = ReleaseService(self.db)
            self.evaluations = EvaluationService(self.db, self.releases)
            self.evaluation_recovery = self.evaluations.recover_interrupted_runs()
            self.maintenance = MaintenanceService(self.db, self.settings)
            self.taobao = TaobaoIntegrationService(self.db, self.settings)
            self.channel_adapters = ChannelAdapterRegistry()
            self.channel_adapters.register(
                TaobaoChannelAdapter(self.taobao, self.settings)
            )
            self.mockchat: MockChatChannelAdapter | None = None
            if self.settings.mockchat_enabled:
                self.mockchat = MockChatChannelAdapter(self.db, self.settings)
                self.channel_adapters.register(self.mockchat)

            self._checkpoint_connection = sqlite3.connect(
                self.settings.checkpoint_db_path,
                check_same_thread=False,
            )
            self.checkpointer = SqliteSaver(self._checkpoint_connection)
            builder = build_graph(
                settings=self.settings,
                db=self.db,
                knowledge=self.knowledge,
                model=self.model,
                handoffs=self.handoffs,
                tools=self.tools,
                sops=self.sops,
                contexts=self.contexts,
                memory=self.memory,
            )
            self.graph = builder.compile(checkpointer=self.checkpointer)
            self.channel_agents = ChannelAgentRuntime(
                self.db,
                self.settings,
                self.releases,
                self.handoffs,
                self.channel_adapters,
                self.chat,
            )
            self.taobao.set_delivery_observer(self.channel_agents.observe_delivery)
        except Exception:
            self._runtime_lock.release()
            raise

    def chat(
        self,
        principal: Principal,
        session_id: str,
        message: str,
        context: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        execution_mode: str = "live",
        source_type: str = "api",
        source_reference: str | None = None,
    ) -> ChatResponse:
        if execution_mode not in {"live", "shadow"}:
            raise ValueError("agent execution mode must be live or shadow")
        internal_session_id = self.db.resolve_session(
            tenant_id=principal.tenant_id,
            client_id=principal.client_id,
            external_session_id=session_id,
            subject_hash=principal.subject_hash,
            source_type=source_type,
            source_reference=source_reference,
        )
        safe_message, input_redacted = redact_sensitive(message)
        untrusted_context = dict(context or {})
        untrusted_context.pop("authorized", None)
        trusted_context = sanitize_context(untrusted_context)
        if principal.can_supply_order_context:
            trusted_context["authorized"] = True
        else:
            for field in (
                "order_id",
                "order_status",
                "logistics_status",
                "carrier",
                "tracking_last_event",
            ):
                trusted_context.pop(field, None)

        invocation: dict[str, Any] | None = None
        if idempotency_key is not None:
            invocation = self._prepare_invocation(
                principal=principal,
                internal_session_id=internal_session_id,
                idempotency_key=idempotency_key,
                safe_message=safe_message,
                trusted_context=trusted_context,
                execution_mode=execution_mode,
            )
            if invocation["status"] == "completed":
                return self._invocation_response(invocation)

        started = time.perf_counter()
        try:
            state = self.graph.invoke(
                {
                    "session_id": internal_session_id,
                    "external_session_id": session_id,
                    "tenant_id": principal.tenant_id,
                    "client_id": principal.client_id,
                    "execution_mode": execution_mode,
                    "invocation_id": invocation["id"] if invocation else None,
                    "trace_id": invocation["trace_id"] if invocation else None,
                    "message_id": invocation["assistant_message_id"] if invocation else None,
                    "user_message_id": invocation["user_message_id"] if invocation else None,
                    "user_input": safe_message,
                    "input_redacted": input_redacted,
                    "context": trusted_context,
                },
                config={"configurable": {"thread_id": internal_session_id}},
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1000
            failure_trace = f"trace-error-{uuid.uuid4().hex}"
            self.db.record_metric(
                trace_id=failure_trace,
                tenant_id=principal.tenant_id,
                session_id=internal_session_id,
                intent="unknown",
                route_reason="unhandled_error",
                success=False,
                model_fallback=False,
                requires_human=True,
                duration_ms=duration_ms,
            )
            self.db.audit(
                "chat.failed",
                principal.client_id,
                failure_trace,
                {"error_type": type(exc).__name__},
                principal.tenant_id,
            )
            if invocation is not None:
                with self.db._write_lock, self.db.connect() as conn:
                    conn.execute(
                        """
                        UPDATE agent_invocations SET last_error=?, updated_at=?
                        WHERE id=? AND tenant_id=? AND status='running'
                        """,
                        (
                            f"{type(exc).__name__}: {str(exc)[:300]}",
                            utc_now(),
                            invocation["id"],
                            principal.tenant_id,
                        ),
                    )
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        self.db.record_metric(
            trace_id=state["trace_id"],
            tenant_id=principal.tenant_id,
            session_id=internal_session_id,
            intent=state["intent"],
            route_reason=state["route_reason"],
            success=True,
            model_fallback=state["model_fallback"],
            requires_human=state["requires_human"],
            duration_ms=duration_ms,
        )
        if invocation is not None:
            with self.db.connect() as conn:
                saved_invocation = conn.execute(
                    "SELECT * FROM agent_invocations WHERE id=? AND tenant_id=?",
                    (invocation["id"], principal.tenant_id),
                ).fetchone()
            if saved_invocation is None or saved_invocation["status"] != "completed":
                raise RuntimeError("idempotent agent invocation did not reach a durable result")
            return self._invocation_response(dict(saved_invocation))

        return self._response_from_state(state, session_id)

    def chat_stream(
        self,
        principal: Principal,
        session_id: str,
        message: str,
        context: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None,
    ) -> Iterator[dict[str, Any]]:
        internal_session_id = self.db.resolve_session(
            tenant_id=principal.tenant_id,
            client_id=principal.client_id,
            external_session_id=session_id,
            subject_hash=principal.subject_hash,
        )
        safe_message, input_redacted = redact_sensitive(message)
        untrusted_context = dict(context or {})
        untrusted_context.pop("authorized", None)
        trusted_context = sanitize_context(untrusted_context)
        if principal.can_supply_order_context:
            trusted_context["authorized"] = True
        else:
            for field in (
                "order_id",
                "order_status",
                "logistics_status",
                "carrier",
                "tracking_last_event",
            ):
                trusted_context.pop(field, None)

        invocation: dict[str, Any] | None = None
        if idempotency_key is not None:
            invocation = self._prepare_invocation(
                principal=principal,
                internal_session_id=internal_session_id,
                idempotency_key=idempotency_key,
                safe_message=safe_message,
                trusted_context=trusted_context,
                execution_mode="live",
            )
            if invocation["status"] == "completed":
                response = self._invocation_response(invocation)
                yield {
                    "event": "meta",
                    "session_id": response.session_id,
                    "message_id": response.message_id,
                    "trace_id": response.trace_id,
                }
                yield {
                    "event": "delta",
                    "text": response.answer,
                    "replay": True,
                }
                yield {"event": "result", "response": response.model_dump()}
                return

        config = {"configurable": {"thread_id": internal_session_id}}
        started = time.perf_counter()
        state = self.graph.invoke(
            {
                "session_id": internal_session_id,
                "external_session_id": session_id,
                "tenant_id": principal.tenant_id,
                "client_id": principal.client_id,
                "execution_mode": "live",
                "invocation_id": invocation["id"] if invocation else None,
                "trace_id": invocation["trace_id"] if invocation else None,
                "message_id": invocation["assistant_message_id"] if invocation else None,
                "user_message_id": invocation["user_message_id"] if invocation else None,
                "user_input": safe_message,
                "input_redacted": input_redacted,
                "context": trusted_context,
            },
            config=config,
            interrupt_before=["generate"],
        )
        yield {
            "event": "meta",
            "session_id": session_id,
            "message_id": state["message_id"],
            "trace_id": state["trace_id"],
        }

        if "generate" in self.graph.get_state(config).next:
            deltas, model_fallback, trace_step = self._generation_deltas(state)
            parts: list[str] = []
            for delta in deltas:
                parts.append(delta)
                yield {"event": "delta", "text": delta}
            draft = "".join(parts).strip()
            generation_state = {
                **state,
                "draft": draft,
                "model_fallback": model_fallback,
                "model_retry_advised": False,
                "trace": [*state["trace"], trace_step],
            }
            verified = verify_response(generation_state)
            self.graph.update_state(
                config,
                {
                    "draft": draft,
                    "model_fallback": model_fallback,
                    "model_retry_advised": False,
                    "trace": generation_state["trace"],
                    **verified,
                },
                as_node="verify",
            )
            state = self.graph.invoke(None, config=config)

        duration_ms = (time.perf_counter() - started) * 1000
        self.db.record_metric(
            trace_id=state["trace_id"],
            tenant_id=principal.tenant_id,
            session_id=internal_session_id,
            intent=state["intent"],
            route_reason=state["route_reason"],
            success=True,
            model_fallback=state["model_fallback"],
            requires_human=state["requires_human"],
            duration_ms=duration_ms,
        )
        response = self._response_from_state(state, session_id)
        yield {"event": "result", "response": response.model_dump()}

    def _generation_deltas(
        self,
        state: dict[str, Any],
    ) -> tuple[Iterator[str], bool, str]:
        verified_result = (
            state.get("tool_result")
            if state.get("tool_result", {}).get("postcondition_met")
            else None
        )
        if not state.get("retrieved") and not verified_result:
            return (
                iter((MODEL_UNAVAILABLE_HANDOFF_ANSWER,)),
                True,
                "generate:no_evidence",
            )

        top_document = state["retrieved"][0] if state.get("retrieved") else None
        if (
            top_document
            and state["decision"].get("reason") == "approved_knowledge_reuse"
            and self.settings.rag_direct_approved_answer
            and top_document["source"].startswith("evolution:")
            # 精确匹配才复用（对齐 graph deliberate 同逻辑）
            and normalize_text(top_document["question"])
            == normalize_text(state["normalized_input"])
        ):
            return iter((top_document["answer"],)), False, "generate:approved_knowledge"

        total = int(
            self.settings.model_context_limit_tokens
            * self.settings.context_budget_ratio
        )
        available = max(
            0,
            total
            - count_tokens(SYSTEM_PROMPT)
            - count_tokens(state["normalized_input"]),
        )
        messages = build_messages(
            question=state["normalized_input"],
            documents=state["retrieved"],
            context=state["context_bundle"],
            history=state["context_bundle"].get("recent_history", []),
            verified_tool_result=verified_result,
            knowledge_budget_tokens=available * 6 // 10,
            prompt_variant=(state.get("intent_routing") or {}).get("prompt_variant"),
        )
        return self.model.stream_generate(messages), False, "generate:stream"

    @staticmethod
    def _response_from_state(
        state: dict[str, Any],
        session_id: str,
    ) -> ChatResponse:
        sources = [
            SourceItem(
                id=document["id"],
                category=document["category"],
                source=document["source"],
                version=document["version"],
                score=document["score"],
            )
            for document in state.get("retrieved", [])
        ]
        return ChatResponse(
            message_id=state["message_id"],
            trace_id=state["trace_id"],
            session_id=session_id,
            answer=state["answer"],
            intent=state["intent"],
            risk_level=state["risk_level"],
            requires_human=state["requires_human"],
            reason=state["route_reason"],
            sources=sources,
            model_fallback=state["model_fallback"],
            handoff_id=state.get("handoff_id"),
            handoff_status=state.get("handoff_status"),
            sop_id=(state.get("active_sop") or {}).get("id"),
            sop_version=(state.get("active_sop") or {}).get("version"),
            context_snapshot_id=state.get("context_snapshot_id"),
            context_readiness=state.get("context_readiness"),
            evidence_ids=state.get("context_evidence_ids", []),
        )

    def _prepare_invocation(
        self,
        *,
        principal: Principal,
        internal_session_id: str,
        idempotency_key: str,
        safe_message: str,
        trusted_context: dict[str, Any],
        execution_mode: str,
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("agent idempotency key must contain 1 to 200 characters")
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "session_id": internal_session_id,
                    "message": safe_message,
                    "context": trusted_context,
                    "execution_mode": execution_mode,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        stable = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"yunpai:{principal.tenant_id}:{principal.client_id}:{idempotency_key}",
        ).hex
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_invocations
                WHERE tenant_id=? AND client_id=? AND idempotency_key=?
                """,
                (principal.tenant_id, principal.client_id, idempotency_key),
            ).fetchone()
            if row is None:
                invocation_id = f"invocation-{stable}"
                conn.execute(
                    """
                    INSERT INTO agent_invocations(
                        id, tenant_id, client_id, session_id, idempotency_key,
                        request_hash, trace_id, user_message_id, assistant_message_id,
                        status, response_json, attempt_count, last_error,
                        created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, 1, NULL,
                              ?, ?, NULL)
                    """,
                    (
                        invocation_id,
                        principal.tenant_id,
                        principal.client_id,
                        internal_session_id,
                        idempotency_key,
                        request_hash,
                        f"trace-{stable}",
                        f"msg-user-{stable}",
                        f"msg-{stable}",
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM agent_invocations WHERE id=?", (invocation_id,)
                ).fetchone()
            else:
                if (
                    row["session_id"] != internal_session_id
                    or row["request_hash"] != request_hash
                ):
                    raise SessionScopeError(
                        "agent idempotency key is already bound to another request",
                        code="idempotency_key_conflict",
                    )
                if row["status"] == "running":
                    conn.execute(
                        """
                        UPDATE agent_invocations
                        SET attempt_count=attempt_count+1, last_error=NULL, updated_at=?
                        WHERE id=? AND status='running'
                        """,
                        (now, row["id"]),
                    )
                    row = conn.execute(
                        "SELECT * FROM agent_invocations WHERE id=?", (row["id"],)
                    ).fetchone()
        if row is None:
            raise RuntimeError("agent invocation was not persisted")
        return dict(row)

    @staticmethod
    def _invocation_response(invocation: dict[str, Any]) -> ChatResponse:
        payload = invocation.get("response_json")
        if not payload:
            raise RuntimeError("completed agent invocation has no response")
        return ChatResponse.model_validate(json.loads(str(payload)))

    def run_release_replay(
        self,
        tenant_id: str,
        release_id: str,
        request: ReleaseReplayRequest,
        actor: str,
    ) -> dict[str, Any]:
        self.releases.get_policy(tenant_id, release_id)
        self.settings.data_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="yunpai-replay-", dir=self.settings.data_dir.parent
        ) as temporary:
            replay_data_dir = Path(temporary)
            with self.db.connect() as source, closing(
                sqlite3.connect(replay_data_dir / "agent.sqlite3")
            ) as destination:
                source.backup(destination)
            replay_settings = replace(
                self.settings,
                data_dir=replay_data_dir,
                backup_dir=None,
                outbox_worker_enabled=False,
                channel_agent_worker_enabled=False,
                taobao_auto_reply_enabled=False,
                release_gate_required=False,
                competitive_monitor_worker_enabled=False,
                handoff_dispatch_worker_enabled=False,
            )
            replay_service = AgentService(replay_settings)
            try:
                replay_client_id = (
                    "release-replay-"
                    + uuid.uuid5(uuid.NAMESPACE_URL, tenant_id).hex
                )
                now = time.time_ns().to_bytes(16, "big")
                with replay_service.db.connect() as conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO api_clients(
                            id, tenant_id, name, key_salt, key_hash,
                            key_iterations, can_supply_order_context, status,
                            created_at, updated_at, role
                        ) VALUES (?, ?, 'Isolated release replay', ?, ?, 1, 0,
                                  'active', datetime('now'), datetime('now'), 'client')
                        """,
                        (replay_client_id, tenant_id, now, now),
                    )
                principal = Principal(
                    tenant_id=tenant_id,
                    client_id=replay_client_id,
                    subject_hash="release-replay-subject",
                    can_supply_order_context=False,
                )
                sequence = 0

                def runner(case):
                    nonlocal sequence
                    sequence += 1
                    return replay_service.chat(
                        principal,
                        f"replay:{release_id}:{sequence}:{case.case_id}",
                        case.message,
                        case.context,
                        source_type="evaluation",
                        source_reference=release_id,
                    )

                return self.releases.run_replay(
                    tenant_id, release_id, request, actor, runner
                )
            finally:
                replay_service.close()

    def run_evaluation_suite(
        self,
        tenant_id: str,
        suite_id: str,
        request: EvaluationRunRequest,
        actor: str,
    ) -> dict[str, Any]:
        self.evaluations.get_suite(tenant_id, suite_id, include_cases=False)
        self.settings.data_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="yunpai-evaluation-", dir=self.settings.data_dir.parent
        ) as temporary:
            evaluation_data_dir = Path(temporary)
            with self.db.connect() as source, closing(
                sqlite3.connect(evaluation_data_dir / "agent.sqlite3")
            ) as destination:
                source.backup(destination)
            evaluation_settings = replace(
                self.settings,
                data_dir=evaluation_data_dir,
                backup_dir=None,
                outbox_worker_enabled=False,
                channel_agent_worker_enabled=False,
                taobao_auto_reply_enabled=False,
                release_gate_required=False,
                competitive_monitor_worker_enabled=False,
                handoff_dispatch_worker_enabled=False,
            )
            evaluation_service = AgentService(evaluation_settings)
            try:
                evaluation_client_id = (
                    "evaluation-" + uuid.uuid5(uuid.NAMESPACE_URL, tenant_id).hex
                )
                salt = time.time_ns().to_bytes(16, "big")
                with evaluation_service.db.connect() as conn:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO api_clients(
                            id, tenant_id, name, key_salt, key_hash,
                            key_iterations, can_supply_order_context, status,
                            created_at, updated_at, role
                        ) VALUES (?, ?, 'Isolated customer evaluation', ?, ?, 1, 1,
                                  'active', datetime('now'), datetime('now'), 'client')
                        """,
                        (evaluation_client_id, tenant_id, salt, salt),
                    )
                principal = Principal(
                    tenant_id=tenant_id,
                    client_id=evaluation_client_id,
                    subject_hash="evaluation-subject",
                    can_supply_order_context=True,
                )

                def runner(case: dict[str, Any]) -> list[ChatResponse]:
                    responses: list[ChatResponse] = []
                    session_id = f"evaluation:{suite_id}:{case['case_key']}"
                    for index, turn in enumerate(case["turns"], start=1):
                        responses.append(
                            evaluation_service.chat(
                                principal,
                                session_id,
                                turn["message"],
                                turn["context"],
                                idempotency_key=f"{request.run_key}:{case['case_key']}:{index}",
                                execution_mode="live",
                                source_type="evaluation",
                                source_reference=request.run_key,
                            )
                        )
                    return responses

                return self.evaluations.run_suite(
                    tenant_id, suite_id, request, actor, runner
                )
            finally:
                evaluation_service.close()

    def _import_knowledge_assets(self) -> dict[str, int]:
        """把 02_clean 资产层知识导入运行时表（M3 接入，幂等）。

        资产层缺失/损坏时降级（日志警告，不阻塞启动）。
        进程级缓存：同一 db 路径只导一次（测试/多实例不重复导入）。
        KG_IMPORT_ENABLED=false 时跳过（测试提速用）。
        """
        if not self.settings.kg_import_enabled:
            return {"imported": 0, "updated": 0, "update_failed": 0, "skipped_entity": 0,
                    "skipped_existing": 0, "skipped_foreign": 0, "seller_default_store_count": 0}
        cache_key = str(self.settings.app_db_path)
        cached = _kg_import_cache.get(cache_key)
        if cached is not None:
            self.kg_import_cached = True
            return cached
        from .knowledge_engine.loader import load_clean_dir
        from .knowledge_engine.runtime_bridge import import_to_runtime

        clean_dir = (
            Path(__file__).resolve().parents[2]
            / "knowledge_graph_output"
            / "02_clean"
        )
        if not clean_dir.is_dir():
            logger.warning("knowledge assets missing: %s (skip kg-* import)", clean_dir)
            stats = {"imported": 0, "updated": 0, "update_failed": 0, "skipped_entity": 0,
                     "skipped_existing": 0, "skipped_foreign": 0, "seller_default_store_count": 0}
            _kg_import_cache[cache_key] = stats
            return stats
        try:
            # ⑥ 存量库惰性重租户化：早期版本把所有 kg-* 资产挂在 bootstrap 租户下。
            # 多租户修复后（P2-1+⑤）general/无店铺资产应为 NULL 全局行——这里做一次性
            # 幂等迁移：bootstrap 挂载的全局层资产行改 NULL，冲突行（同键已有 NULL
            # active）置 retired 防撞 v33 唯一索引。有店铺 seller 行保持租户不变。
            self._retrofit_global_asset_tenants()
            items = load_clean_dir(clean_dir)
            stats = import_to_runtime(
                items,
                self.knowledge,
                tenant_id=self.settings.bootstrap_tenant_id,
                default_store_id=self.settings.bootstrap_tenant_id,
                # 多租户（P1-2）：appliance 自身是全局知识的唯一写入口，
                # 允许热更新全局行；租户端点（import-assets API）不传此旗标
                allow_global_update=True,
            )
            logger.info(
                "knowledge assets imported: imported=%d skipped_entity=%d skipped_existing=%d seller_default_store=%d",
                stats["imported"],
                stats["skipped_entity"],
                stats["skipped_existing"],
                stats["seller_default_store_count"],
            )
            _kg_import_cache[cache_key] = stats
            return stats
        except Exception as exc:
            logger.warning("knowledge assets import failed (non-fatal): %s", exc)
            stats = {"imported": 0, "updated": 0, "update_failed": 0, "skipped_entity": 0,
                     "skipped_existing": 0, "skipped_foreign": 0, "seller_default_store_count": 0}
            _kg_import_cache[cache_key] = stats
            return stats

    def _retrofit_global_asset_tenants(self) -> None:
        """⑥ 存量库惰性重租户化（一次性幂等迁移）。

        早期版本把 02_clean 资产全部挂 bootstrap 租户；多租户修复（P2-1+⑤）
        后全局层资产（layer IN platform/industry，及无店铺 seller）应为
        tenant_id NULL。本迁移把 bootstrap 挂载的全局层 kg-* 行改 NULL；
        同键已有 NULL active 行时置 retired（防撞 v33 唯一索引）。
        有店铺 seller 行不动。迁移失败不阻塞启动（下次启动重试）。
        """
        try:
            with self.db._write_lock, self.db.connect() as conn:
                # 冲突防护：同键已有 NULL active 行的 bootstrap 行 → 置 retired
                conn.execute(
                    """
                    UPDATE knowledge SET status='retired', record_version=record_version+1,
                        updated_at=?
                    WHERE tenant_id=? AND id LIKE 'kg-%' AND status='active'
                      AND layer IN ('platform', 'industry', 'store')
                      AND store_id IS NULL
                      AND EXISTS (
                          SELECT 1 FROM knowledge g
                          WHERE g.tenant_id IS NULL AND g.knowledge_key=knowledge.knowledge_key
                            AND g.status='active'
                      )
                    """,
                    (utc_now(), self.settings.bootstrap_tenant_id),
                )
                # 无冲突的全局层行 → 改 NULL（全局可见）
                cursor = conn.execute(
                    """
                    UPDATE knowledge SET tenant_id=NULL, record_version=record_version+1,
                        updated_at=?
                    WHERE tenant_id=? AND id LIKE 'kg-%' AND status='active'
                      AND layer IN ('platform', 'industry', 'store')
                      AND store_id IS NULL
                    """,
                    (utc_now(), self.settings.bootstrap_tenant_id),
                )
                if cursor.rowcount:
                    logger.info(
                        "retrofit: %d 条 bootstrap 挂载的全局资产行已重租户化为 NULL（多租户修复）",
                        cursor.rowcount,
                    )
        except Exception:
            # 迁移失败不阻塞启动（下次启动幂等重试）
            logger.exception("retrofit global asset tenants failed (non-fatal)")

    def health(self) -> dict[str, Any]:
        model_ok, model_detail = self.model.health()
        knowledge_count = self.knowledge.count_active()
        schema_version = self.db.schema_version()
        foundation_ok = knowledge_count >= 100 and schema_version == Database.SCHEMA_VERSION
        auth_ok = self.auth.configured
        admin_ok = self.auth.admin_configured
        return {
            "status": "ok" if foundation_ok and auth_ok and admin_ok else "degraded",
            "mode": "live_model" if self.settings.model_enabled else "framework",
            "model": {
                "enabled": self.settings.model_enabled,
                "ok": model_ok,
                "detail": model_detail,
                "provider": self.settings.model_provider,
                "name": self.settings.model_name,
                "thinking_enabled": self.settings.model_thinking_enabled,
                "streaming": self.settings.model_streaming,
                "required_for_foundation_health": False,
            },
            "authentication": {"required": self.settings.auth_required, "configured": auth_ok},
            "knowledge": {"active_documents": knowledge_count},
            "sops": {
                "active": len(self.sops.catalog_for_context(self.settings.bootstrap_tenant_id)),
            },
            "database": {"schema_version": schema_version},
            "administrator": {
                "configured": admin_ok,
                "authentication_required": self.settings.admin_auth_required,
                "local_bypass": not self.settings.admin_auth_required,
                "local_principal_id": (
                    self.settings.bootstrap_admin_id
                    if not self.settings.admin_auth_required
                    else None
                ),
            },
            "storage": "sqlite",
            "orchestrator": "langgraph",
            "registered_tools": len(self.tools),
            "business_modules": self.operations.modules(),
            "competitive_monitoring": self.competitive_monitor_worker_status(),
            "session_idle": self.session_idle_worker_status(),
            "handoff_sla": self.handoff_sla_worker_status(),
            "knowledge_worker": self.knowledge_worker_status(),
            "handoff_dispatch": {
                **self.handoff_dispatch.summary(
                    tenant_id=self.settings.bootstrap_tenant_id
                ),
                "worker": self.handoff_dispatch_worker_status(),
                "recovered_jobs": self.handoff_dispatch_recovery,
            },
            "connectors": self.operations.connector_catalog(),
            "taobao": self.taobao.capabilities(self.settings.bootstrap_tenant_id),
            "outbox": self.taobao.outbox_summary(self.settings.bootstrap_tenant_id),
            "channel_agent": self.channel_agents.summary(
                self.settings.bootstrap_tenant_id
            ),
            "release_gate": {
                "required": self.settings.release_gate_required,
                "active_policies": len(
                    self.releases.list_policies(
                        self.settings.bootstrap_tenant_id, status="active"
                    )
                ),
            },
            "evaluations": {
                "runner_version": self.evaluations.RUNNER_VERSION,
                "recovered_interrupted_runs": self.evaluation_recovery["recovered"],
            },
        }

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        free_mb = shutil.disk_usage(self.settings.data_dir).free // (1024 * 1024)
        checks = {
            "schema_current": self.db.schema_version() == Database.SCHEMA_VERSION,
            "client_authentication": self.auth.configured,
            "administrator_authentication": self.auth.admin_configured,
            "knowledge_seeded": self.knowledge.count_active() >= 100,
            "disk_free": free_mb >= self.settings.min_free_disk_mb,
            "checkpoint_store": self._checkpoint_connection.execute("SELECT 1").fetchone()[0] == 1,
            "model_configuration": (
                not self.settings.model_enabled or self.model.health()[0]
            ),
            "business_modules": any(
                item["status"] == "available" for item in self.operations.modules()
            ),
            "virtual_connector": self.operations.connectors.get(
                "virtual_taobao"
            ).test_connection().ok,
            "outbox_worker": (
                not self.settings.outbox_worker_enabled
                or self.taobao.outbox_worker_status()["running"]
            ),
            "competitive_monitor_worker": (
                not self.settings.competitive_monitor_worker_enabled
                or self.competitive_monitor_worker_status()["running"]
            ),
            "session_idle_worker": self.session_idle_worker_status()["running"],
            "handoff_sla_worker": (
                not self.settings.handoff_sla_worker_enabled
                or self.handoff_sla_worker_status()["running"]
            ),
            "handoff_dispatch_worker": (
                not self.settings.handoff_dispatch_worker_enabled
                or self.handoff_dispatch_worker_status()["running"]
            ),
            "channel_agent_worker": (
                not self.settings.taobao_auto_reply_enabled
                or (
                    self.settings.channel_agent_worker_enabled
                    and self.channel_agents.worker_status()["running"]
                )
            ),
            "release_gate": (
                not self.settings.taobao_auto_reply_enabled
                or not self.settings.release_gate_required
                or bool(
                    self.releases.list_policies(
                        self.settings.bootstrap_tenant_id, status="active"
                    )
                )
            ),
        }
        ready = all(checks.values())
        return ready, {
            "status": "ready" if ready else "not_ready",
            "checks": checks,
            "disk_free_mb": free_mb,
            "minimum_disk_free_mb": self.settings.min_free_disk_mb,
        }

    def start(self) -> None:
        self.taobao.start_outbox_worker()
        self.channel_agents.start_worker()
        self.start_competitive_monitor_worker()
        self.start_session_idle_worker()
        self.start_handoff_sla_worker()
        self.start_handoff_dispatch_worker()
        self.start_knowledge_worker()

    # ---------- 知识库自维护 worker（A3：梦循环 + 每日评测） ----------

    KNOWLEDGE_WORKER_POLL_SECONDS = 3600.0  # 每小时检查一次调度窗口
    KNOWLEDGE_DREAM_INTERVAL_SECONDS = 86400.0  # 梦循环每天一次
    KNOWLEDGE_EVAL_INTERVAL_SECONDS = 86400.0  # 检索评测每天一次

    def start_knowledge_worker(self) -> None:
        if not self.settings.kg_dream_worker_enabled:
            return
        with self._knowledge_worker_lock:
            if (
                self._knowledge_worker_thread is not None
                and self._knowledge_worker_thread.is_alive()
            ):
                return
            self._knowledge_worker_stop.clear()
            self._knowledge_worker_thread = threading.Thread(
                target=self._knowledge_worker_loop,
                name="knowledge-worker",
                daemon=True,
            )
            self._knowledge_worker_thread.start()

    def stop_knowledge_worker(self) -> None:
        with self._knowledge_worker_lock:
            thread = self._knowledge_worker_thread
            self._knowledge_worker_stop.set()
        if thread is not None:
            thread.join(timeout=5)
        with self._knowledge_worker_lock:
            if thread is None or not thread.is_alive():
                self._knowledge_worker_thread = None

    def knowledge_worker_status(self) -> dict[str, Any]:
        thread = self._knowledge_worker_thread
        return {
            "enabled": self.settings.kg_dream_worker_enabled,
            "running": bool(thread and thread.is_alive()),
            "poll_seconds": self.KNOWLEDGE_WORKER_POLL_SECONDS,
            "dream_interval_seconds": self.KNOWLEDGE_DREAM_INTERVAL_SECONDS,
            "eval_interval_seconds": self.KNOWLEDGE_EVAL_INTERVAL_SECONDS,
            "cycles": self._knowledge_worker_cycles,
            "last_run_at": self._knowledge_worker_last_run_at,
            "last_error": self._knowledge_worker_last_error,
            "last_report": self._knowledge_worker_last_report,
        }

    def _knowledge_worker_loop(self) -> None:
        while not self._knowledge_worker_stop.is_set():
            try:
                self._run_knowledge_cycle()
                self._knowledge_worker_cycles += 1
                self._knowledge_worker_last_run_at = utc_now()
                self._knowledge_worker_last_error = None
            except Exception as exc:
                self._knowledge_worker_last_error = (
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                )
                self.db.audit(
                    "knowledge.worker_failed",
                    "knowledge-worker",
                    "scheduler",
                    {"error_type": type(exc).__name__, "error": str(exc)[:300]},
                    self.settings.bootstrap_tenant_id,
                )
            self._knowledge_worker_stop.wait(self.KNOWLEDGE_WORKER_POLL_SECONDS)

    def _run_knowledge_cycle(self) -> dict[str, Any]:
        """一次知识库维护周期：梦循环（合并结论落库）+ 检索评测。

        失败不抛错（单点失败不影响整个 worker 存活），通过返回值报告。
        """
        from .knowledge_engine.scheduler import run_dream_cycle_once, run_evaluation_once

        report: dict[str, Any] = {}
        try:
            dream = run_dream_cycle_once(
                persist=True,
                knowledge_base=self.knowledge,
            )
            report["dream_cycle"] = dream
        except Exception as exc:
            report["dream_cycle"] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        try:
            report["retrieval_eval"] = run_evaluation_once()
        except Exception as exc:
            report["retrieval_eval"] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        self._knowledge_worker_last_report = report
        return report

    def start_session_idle_worker(self) -> None:
        with self._session_idle_worker_lock:
            if (
                self._session_idle_worker_thread is not None
                and self._session_idle_worker_thread.is_alive()
            ):
                return
            self._session_idle_worker_stop.clear()
            self._session_idle_worker_thread = threading.Thread(
                target=self._session_idle_worker_loop,
                name="session-idle-worker",
                daemon=True,
            )
            self._session_idle_worker_thread.start()

    def stop_session_idle_worker(self) -> None:
        with self._session_idle_worker_lock:
            thread = self._session_idle_worker_thread
            self._session_idle_worker_stop.set()
        if thread is not None:
            thread.join(timeout=5)
        with self._session_idle_worker_lock:
            if thread is None or not thread.is_alive():
                self._session_idle_worker_thread = None

    def session_idle_worker_status(self) -> dict[str, Any]:
        thread = self._session_idle_worker_thread
        return {
            "running": bool(thread and thread.is_alive()),
            "poll_seconds": self.SESSION_IDLE_WORKER_POLL_SECONDS,
            "timeout_minutes": self.settings.session_idle_timeout_minutes,
            "cycles": self._session_idle_worker_cycles,
            "closed": self._session_idle_worker_closed,
            "last_run_at": self._session_idle_worker_last_run_at,
            "last_error": self._session_idle_worker_last_error,
        }

    def _session_idle_worker_loop(self) -> None:
        while not self._session_idle_worker_stop.is_set():
            try:
                report = self.maintenance.close_idle_sessions()
                self._session_idle_worker_cycles += 1
                self._session_idle_worker_closed += int(report["closed"])
                self._session_idle_worker_last_run_at = str(report["run_at"])
                self._session_idle_worker_last_error = None
            except Exception as exc:
                self._session_idle_worker_last_error = (
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                )
                self.db.audit(
                    "session.idle_worker_failed",
                    "session-idle-worker",
                    "scheduler",
                    {"error_type": type(exc).__name__},
                    self.settings.bootstrap_tenant_id,
                )
            self._session_idle_worker_stop.wait(
                self.SESSION_IDLE_WORKER_POLL_SECONDS
            )

    def start_competitive_monitor_worker(self) -> None:
        if not self.settings.competitive_monitor_worker_enabled:
            return
        with self._competitive_worker_lock:
            if (
                self._competitive_worker_thread is not None
                and self._competitive_worker_thread.is_alive()
            ):
                return
            self._competitive_worker_stop.clear()
            self._competitive_worker_thread = threading.Thread(
                target=self._competitive_monitor_worker_loop,
                name="competitive-monitor-worker",
                daemon=True,
            )
            self._competitive_worker_thread.start()

    def stop_competitive_monitor_worker(self) -> None:
        with self._competitive_worker_lock:
            thread = self._competitive_worker_thread
            self._competitive_worker_stop.set()
        if thread is not None:
            thread.join(timeout=5)
        with self._competitive_worker_lock:
            if thread is None or not thread.is_alive():
                self._competitive_worker_thread = None

    def competitive_monitor_worker_status(self) -> dict[str, Any]:
        thread = self._competitive_worker_thread
        return {
            "enabled": self.settings.competitive_monitor_worker_enabled,
            "running": bool(thread and thread.is_alive()),
            "poll_seconds": self.settings.competitive_monitor_poll_seconds,
            "cycles": self._competitive_worker_cycles,
            "evaluated": self._competitive_worker_evaluated,
            "last_run_at": self._competitive_worker_last_run_at,
            "last_error": self._competitive_worker_last_error,
        }

    def _competitive_monitor_worker_loop(self) -> None:
        while not self._competitive_worker_stop.is_set():
            try:
                report = self.operations.competitive.evaluate_all_tenants()
                self._competitive_worker_cycles += 1
                self._competitive_worker_evaluated += int(report["evaluated"])
                self._competitive_worker_last_run_at = utc_now()
                if report["errors"]:
                    self._competitive_worker_last_error = "; ".join(
                        f"{item['tenant_id']}:{item['error_type']}" for item in report["errors"]
                    )[:500]
                    for item in report["errors"]:
                        self.db.audit(
                            "competitive.monitor.worker_failed",
                            "competitive-monitor-worker",
                            item["tenant_id"],
                            item,
                            item["tenant_id"],
                        )
                else:
                    self._competitive_worker_last_error = None
            except Exception as exc:
                self._competitive_worker_last_error = (
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                )
                self.db.audit(
                    "competitive.monitor.worker_failed",
                    "competitive-monitor-worker",
                    "scheduler",
                    {"error_type": type(exc).__name__},
                    self.settings.bootstrap_tenant_id,
                )
            self._competitive_worker_stop.wait(
                self.settings.competitive_monitor_poll_seconds
            )

    def start_handoff_sla_worker(self) -> None:
        if not self.settings.handoff_sla_worker_enabled:
            return
        with self._handoff_sla_worker_lock:
            if (
                self._handoff_sla_worker_thread is not None
                and self._handoff_sla_worker_thread.is_alive()
            ):
                return
            self._handoff_sla_worker_stop.clear()
            self._handoff_sla_worker_thread = threading.Thread(
                target=self._handoff_sla_worker_loop,
                name="handoff-sla-worker",
                daemon=True,
            )
            self._handoff_sla_worker_thread.start()

    def stop_handoff_sla_worker(self) -> None:
        with self._handoff_sla_worker_lock:
            thread = self._handoff_sla_worker_thread
            self._handoff_sla_worker_stop.set()
        if thread is not None:
            thread.join(timeout=5)
        with self._handoff_sla_worker_lock:
            if thread is None or not thread.is_alive():
                self._handoff_sla_worker_thread = None

    def handoff_sla_worker_status(self) -> dict[str, Any]:
        thread = self._handoff_sla_worker_thread
        return {
            "enabled": self.settings.handoff_sla_worker_enabled,
            "running": bool(thread and thread.is_alive()),
            "poll_seconds": self.settings.handoff_sla_poll_seconds,
            "cycles": self._handoff_sla_worker_cycles,
            "escalated": self._handoff_sla_worker_escalated,
            "last_run_at": self._handoff_sla_worker_last_run_at,
            "last_error": self._handoff_sla_worker_last_error,
        }

    def _handoff_sla_worker_loop(self) -> None:
        while not self._handoff_sla_worker_stop.is_set():
            try:
                report = self.handoffs.escalate_due()
                self._handoff_sla_worker_cycles += 1
                self._handoff_sla_worker_escalated += int(report["escalated"])
                self._handoff_sla_worker_last_run_at = report["run_at"]
                self._handoff_sla_worker_last_error = None
            except Exception as exc:
                self._handoff_sla_worker_last_error = (
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                )
                self.db.audit(
                    "handoff.sla_worker_failed",
                    "handoff-sla-worker",
                    "scheduler",
                    {"error_type": type(exc).__name__},
                    self.settings.bootstrap_tenant_id,
                )
            self._handoff_sla_worker_stop.wait(
                self.settings.handoff_sla_poll_seconds
            )

    def start_handoff_dispatch_worker(self) -> None:
        if not self.settings.handoff_dispatch_worker_enabled:
            return
        with self._handoff_dispatch_worker_lock:
            if (
                self._handoff_dispatch_worker_thread is not None
                and self._handoff_dispatch_worker_thread.is_alive()
            ):
                return
            self._handoff_dispatch_worker_stop.clear()
            self._handoff_dispatch_worker_thread = threading.Thread(
                target=self._handoff_dispatch_worker_loop,
                name="handoff-dispatch-worker",
                daemon=True,
            )
            self._handoff_dispatch_worker_thread.start()

    def stop_handoff_dispatch_worker(self) -> None:
        with self._handoff_dispatch_worker_lock:
            thread = self._handoff_dispatch_worker_thread
            self._handoff_dispatch_worker_stop.set()
        if thread is not None:
            thread.join(timeout=5)
        with self._handoff_dispatch_worker_lock:
            if thread is None or not thread.is_alive():
                self._handoff_dispatch_worker_thread = None

    def handoff_dispatch_worker_status(self) -> dict[str, Any]:
        thread = self._handoff_dispatch_worker_thread
        return {
            "enabled": self.settings.handoff_dispatch_worker_enabled,
            "running": bool(thread and thread.is_alive()),
            "poll_seconds": self.settings.handoff_dispatch_poll_seconds,
            "cycles": self._handoff_dispatch_worker_cycles,
            "assigned": self._handoff_dispatch_worker_assigned,
            "waiting": self._handoff_dispatch_worker_waiting,
            "failed": self._handoff_dispatch_worker_failed,
            "last_run_at": self._handoff_dispatch_worker_last_run_at,
            "last_error": self._handoff_dispatch_worker_last_error,
        }

    def _handoff_dispatch_worker_loop(self) -> None:
        while not self._handoff_dispatch_worker_stop.is_set():
            try:
                report = self.handoff_dispatch.run_once(
                    worker_id="handoff-dispatch-worker",
                    limit=self.settings.handoff_dispatch_batch_size,
                )
                self._handoff_dispatch_worker_cycles += 1
                self._handoff_dispatch_worker_assigned += int(report["assigned"])
                self._handoff_dispatch_worker_waiting += int(report["waiting"])
                self._handoff_dispatch_worker_failed += int(report["failed"])
                self._handoff_dispatch_worker_last_run_at = str(report["run_at"])
                self._handoff_dispatch_worker_last_error = None
            except Exception as exc:
                self._handoff_dispatch_worker_last_error = (
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                )
                self.db.audit(
                    "handoff.dispatch_worker_failed",
                    "handoff-dispatch-worker",
                    "scheduler",
                    {"error_type": type(exc).__name__},
                    self.settings.bootstrap_tenant_id,
                )
            self._handoff_dispatch_worker_stop.wait(
                self.settings.handoff_dispatch_poll_seconds
            )

    def purge_expired(self, *, actor: str, dry_run: bool) -> dict[str, Any]:
        report = self.maintenance.purge_expired(actor=actor, dry_run=dry_run)
        expired_session_ids = report.pop("expired_session_ids")
        checkpoints_deleted = 0
        if not dry_run:
            for session_id in expired_session_ids:
                self.checkpointer.delete_thread(session_id)
                checkpoints_deleted += 1
        report["checkpoints_deleted"] = checkpoints_deleted
        return report

    def close(self) -> None:
        try:
            self.stop_handoff_dispatch_worker()
            self.stop_handoff_sla_worker()
            self.stop_session_idle_worker()
            self.stop_competitive_monitor_worker()
            self.channel_agents.close()
            self.taobao.close()
            self.model.close()
            self.tools.close()
            self._checkpoint_connection.close()
        finally:
            self._runtime_lock.release()
