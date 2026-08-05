from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RetrievedDocument(TypedDict):
    id: str
    category: str
    intent: str
    question: str
    answer: str
    source: str
    version: int
    score: float
    layer: str
    store_id: str | None
    sku_id: str | None


class AgentState(TypedDict, total=False):
    session_id: str
    external_session_id: str
    tenant_id: str
    client_id: str
    execution_mode: str
    invocation_id: str | None
    user_message_id: str
    user_input: str
    input_redacted: bool
    normalized_input: str
    context: dict[str, Any]
    intent: str
    customer_intent: str | None
    intent_confidence: float | None
    intent_method: str | None
    risk_level: str
    route: str
    route_reason: str
    retrieved: list[RetrievedDocument]
    draft: str
    answer: str
    citations: list[str]
    requires_human: bool
    message_id: str
    trace_id: str
    trace: list[str]
    review_route: str
    model_fallback: bool
    model_retry_advised: bool
    handoff_id: str | None
    handoff_status: str | None
    decision: dict[str, Any]
    decision_mode: str
    selected_tool: str | None
    tool_arguments: dict[str, Any]
    tool_result: dict[str, Any]
    react_step: int
    active_sop: dict[str, Any] | None
    sop_step_run_id: str | None
    context_bundle: dict[str, Any]
    context_snapshot_id: str | None
    context_readiness: str
    context_evidence_ids: list[str]
    context_conflicts: list[dict[str, Any]]


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    message: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict, max_length=16)


class ChatMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict, max_length=16)


class SourceItem(BaseModel):
    id: str
    category: str
    source: str
    version: int
    score: float


class ChatResponse(BaseModel):
    message_id: str
    trace_id: str
    session_id: str
    answer: str
    intent: str
    risk_level: str
    requires_human: bool
    reason: str
    sources: list[SourceItem]
    model_fallback: bool = False
    handoff_id: str | None = None
    handoff_status: str | None = None
    sop_id: str | None = None
    sop_version: int | None = None
    context_snapshot_id: str | None = None
    context_readiness: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class CustomerTestChatRequest(BaseModel):
    """Loopback-only customer-test payload; never used by a production channel."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^customer-test:[A-Za-z0-9_.:-]+$",
    )
    message: str = Field(min_length=1, max_length=4000)
    context: dict[str, Any] = Field(default_factory=dict, max_length=16)


class CustomerTestChatResponse(ChatResponse):
    test_mode: Literal["local_customer_simulation"] = "local_customer_simulation"
    source_type: Literal["simulation"] = "simulation"
    source_reference: str = "local-customer-test"


class CustomerTestCase(BaseModel):
    id: str
    title: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
    expected: str


class FeedbackRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=128)
    rating: Literal[-1, 1]
    corrected_answer: str | None = Field(default=None, max_length=1200)
    note: str | None = Field(default=None, max_length=1000)
    evidence_source: str | None = Field(default=None, min_length=4, max_length=500)
    submitted_by: str = Field(default="operator", min_length=1, max_length=128)


class FeedbackResponse(BaseModel):
    feedback_id: str
    candidate_id: str | None
    status: str


class EvolutionDecision(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class CandidateView(BaseModel):
    id: str
    question: str
    proposed_answer: str
    evidence_source: str | None
    intent: str
    status: str
    gate_passed: bool | None
    gate_report: dict[str, Any] | None
    created_at: str


HandoffStatus = Literal[
    "proposed",
    "accepted",
    "rejected",
    "working",
    "input_required",
    "review",
    "completed",
    "failed",
    "canceled",
]


class HandoffView(BaseModel):
    id: str
    tenant_id: str
    external_session_id: str
    source_type: Literal["api", "channel", "simulation", "evaluation"] = "api"
    source_reference: str | None = None
    message_id: str
    status: HandoffStatus
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    acceptance_criteria: str
    queue_id: str
    queue_key: str
    queue_name: str
    priority: Literal["low", "normal", "high", "urgent"]
    assigned_to: str | None
    assigned_operator_name: str | None = None
    assigned_operator_presence: str | None = None
    deadline_at: str | None
    sla_first_response_at: str | None
    sla_resolution_at: str | None
    sla_status: Literal["on_track", "due_soon", "breached", "met"]
    sla_remaining_seconds: int | None
    first_response_breached: bool
    resolution_breached: bool
    acknowledged_at: str | None
    started_at: str | None
    review_started_at: str | None
    escalated_at: str | None
    escalation_level: int
    escalation_reason: str | None
    max_retries: int
    retry_count: int
    version: int
    created_at: str
    updated_at: str
    completed_at: str | None


class HandoffTransition(BaseModel):
    target_status: HandoffStatus
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=1000)


class HandoffClaimRequest(BaseModel):
    expected_version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=1000)


class HandoffReassignRequest(BaseModel):
    expected_version: int = Field(ge=1)
    assigned_to: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:@-]+$")
    queue_key: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]*$"
    )
    note: str = Field(min_length=2, max_length=1000)


class HandoffEscalateRequest(BaseModel):
    expected_version: int = Field(ge=1)
    queue_key: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]*$"
    )
    note: str = Field(min_length=2, max_length=1000)


class HandoffNoteRequest(BaseModel):
    expected_version: int = Field(ge=1)
    note: str = Field(min_length=2, max_length=1000)


class HandoffQueueUpsert(BaseModel):
    queue_key: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]*$"
    )
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    status: Literal["active", "inactive"] = "active"
    default_priority: Literal["low", "normal", "high", "urgent"] = "normal"
    first_response_sla_minutes: int = Field(default=30, ge=1, le=10080)
    resolution_sla_minutes: int = Field(default=480, ge=1, le=43200)
    max_active_per_operator: int = Field(default=20, ge=1, le=100)
    escalation_queue_key: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]*$"
    )
    match_reasons: list[str] = Field(default_factory=list, max_length=30)
    match_intents: list[str] = Field(default_factory=list, max_length=30)
    match_risk_levels: list[Literal["low", "medium", "high", "critical", "blocked"]] = (
        Field(default_factory=list, max_length=5)
    )
    routing_order: int = Field(default=100, ge=0, le=10000)
    expected_record_version: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_sla_order(self) -> "HandoffQueueUpsert":
        if self.resolution_sla_minutes < self.first_response_sla_minutes:
            raise ValueError("resolution SLA cannot be shorter than first-response SLA")
        return self


class HandoffQueueView(BaseModel):
    id: str
    tenant_id: str
    queue_key: str
    name: str
    description: str
    status: Literal["active", "inactive"]
    default_priority: Literal["low", "normal", "high", "urgent"]
    first_response_sla_minutes: int
    resolution_sla_minutes: int
    max_active_per_operator: int
    escalation_queue_key: str | None
    match_reasons: list[str]
    match_intents: list[str]
    match_risk_levels: list[str]
    routing_order: int
    record_version: int
    open_tasks: int = 0
    assigned_tasks: int = 0
    breached_tasks: int = 0
    total_operators: int = 0
    available_operators: int = 0
    created_by: str
    created_at: str
    updated_at: str


class HandoffEventView(BaseModel):
    id: str
    handoff_id: str
    event_type: str
    from_status: str | None
    to_status: str | None
    from_queue_key: str | None
    to_queue_key: str | None
    from_assignee: str | None
    to_assignee: str | None
    task_version: int
    actor: str
    note: str | None
    created_at: str


class HandoffOperatorQueueAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queue_key: str = Field(
        min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]*$"
    )
    skill_level: int = Field(default=3, ge=1, le=5)
    is_primary: bool = False


class HandoffOperatorUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=2, max_length=160)
    status: Literal["active", "inactive"] = "active"
    presence: Literal["available", "away", "offline"] = "offline"
    dispatch_mode: Literal["automatic", "manual"] = "automatic"
    schedule_mode: Literal["unrestricted", "scheduled"] = "unrestricted"
    max_active_tasks: int = Field(default=20, ge=1, le=100)
    skills: list[str] = Field(default_factory=list, max_length=30)
    queue_assignments: list[HandoffOperatorQueueAssignment] = Field(
        default_factory=list, max_length=50
    )
    presence_ttl_seconds: int = Field(default=3600, ge=60, le=28800)
    expected_record_version: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_assignments(self) -> "HandoffOperatorUpsert":
        queue_keys = [item.queue_key for item in self.queue_assignments]
        if len(queue_keys) != len(set(queue_keys)):
            raise ValueError("operator queue assignments must be unique")
        if sum(item.is_primary for item in self.queue_assignments) > 1:
            raise ValueError("operator can have at most one primary queue")
        if self.status == "active" and not self.queue_assignments:
            raise ValueError("active operator requires at least one queue assignment")
        return self


class HandoffOperatorPresenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    presence: Literal["available", "away", "offline"]
    presence_ttl_seconds: int = Field(default=3600, ge=60, le=28800)
    expected_record_version: int = Field(ge=1)


class HandoffPresenceSessionStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(
        min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    presence: Literal["available", "away"] = "available"
    presence_ttl_seconds: int = Field(default=120, ge=60, le=3600)
    expected_record_version: int = Field(ge=1)


class HandoffOperatorHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(
        min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    sequence: int = Field(ge=1)
    presence: Literal["available", "away"] = "available"
    presence_ttl_seconds: int = Field(default=120, ge=60, le=3600)
    expected_presence_version: int = Field(ge=1)


class HandoffPresenceSessionView(BaseModel):
    session_id: str
    presence_version: int
    sequence: int
    presence_expires_at: str
    operator: "HandoffOperatorView"


class HandoffShiftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "HandoffShiftCreate":
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("shift timestamps must include a UTC offset")
        duration = self.ends_at - self.starts_at
        if duration < timedelta(minutes=15):
            raise ValueError("shift must be at least 15 minutes")
        if duration > timedelta(hours=24):
            raise ValueError("shift cannot be longer than 24 hours")
        return self


class HandoffRecurringShiftCreate(HandoffShiftCreate):
    repeat_every_weeks: int = Field(default=1, ge=1, le=4)
    occurrences: int = Field(ge=2, le=26)


class HandoffShiftCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    note: str = Field(min_length=2, max_length=500)


class HandoffShiftView(BaseModel):
    id: str
    tenant_id: str
    operator_id: str
    starts_at: str
    ends_at: str
    status: Literal["scheduled", "cancelled"]
    record_version: int
    created_by: str
    created_at: str
    updated_at: str


class HandoffOperatorQueueView(BaseModel):
    queue_key: str
    queue_name: str
    skill_level: int
    is_primary: bool
    active_tasks: int = 0


class HandoffOperatorView(BaseModel):
    id: str
    tenant_id: str
    operator_id: str
    display_name: str
    status: Literal["active", "inactive"]
    configured_presence: Literal["available", "away", "offline"]
    effective_presence: Literal["available", "away", "offline"]
    credential_status: Literal["active", "disabled"]
    dispatch_mode: Literal["automatic", "manual"]
    schedule_mode: Literal["unrestricted", "scheduled"]
    on_shift: bool
    next_shift_start: str | None
    max_active_tasks: int
    active_tasks: int
    load_ratio: float
    available_for_claim: bool
    skills: list[str]
    queue_assignments: list[HandoffOperatorQueueView]
    record_version: int
    presence_version: int
    presence_updated_at: str
    presence_expires_at: str | None
    created_by: str
    created_at: str
    updated_at: str


class HandoffAutoAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    note: str = Field(min_length=2, max_length=1000)


class HandoffDispatchJobView(BaseModel):
    id: str
    tenant_id: str
    handoff_id: str
    source_type: Literal["api", "channel", "simulation", "evaluation"] = "api"
    source_reference: str | None = None
    queue_key: str
    priority: Literal["low", "normal", "high", "urgent"]
    status: Literal["pending", "leased", "waiting", "assigned", "cancelled", "failed"]
    attempt_count: int
    available_at: str
    lease_owner: str | None
    lease_expires_at: str | None
    assigned_to: str | None
    last_error: str | None
    record_version: int
    created_at: str
    updated_at: str
    completed_at: str | None


class HandoffDispatchAlertView(BaseModel):
    id: str
    tenant_id: str
    handoff_id: str
    source_type: Literal["api", "channel", "simulation", "evaluation"] = "api"
    source_reference: str | None = None
    queue_key: str
    status: Literal["open", "acknowledged", "resolved"]
    reason: Literal["no_available_operator", "dispatch_error"]
    occurrence_count: int
    detail: dict[str, Any]
    first_seen_at: str
    last_seen_at: str
    acknowledged_by: str | None
    acknowledged_at: str | None
    resolved_at: str | None
    record_version: int


class HandoffDispatchAlertAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    note: str = Field(min_length=2, max_length=500)


class HandoffDispatchRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_record_version: int = Field(ge=1)
    note: str = Field(min_length=2, max_length=500)


class RetentionRequest(BaseModel):
    dry_run: bool = True
