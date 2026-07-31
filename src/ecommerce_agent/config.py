from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def is_loopback_host(value: str) -> bool:
    host = value.strip().lower().strip("[]")
    if host == "localhost":
        return True
    try:
        return ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    model_provider: str
    model_base_url: str
    model_name: str
    model_api_key: str
    model_timeout_seconds: float
    model_max_output_tokens: int
    model_temperature: float
    model_thinking_enabled: bool
    model_streaming: bool
    model_retry_attempts: int
    model_enabled: bool
    model_mock_mode: bool
    model_context_limit_tokens: int
    context_budget_ratio: float
    rag_top_k: int
    rag_min_score: float
    rag_direct_approved_answer: bool
    rag_direct_approved_min_score: float
    max_input_chars: int
    session_history_limit: int
    admin_api_key: str
    admin_auth_required: bool
    bootstrap_admin_id: str
    auth_required: bool
    bootstrap_tenant_id: str
    bootstrap_client_id: str
    bootstrap_client_key: str
    bootstrap_client_can_supply_order_context: bool
    subject_hash_key: str
    session_idle_timeout_minutes: int
    message_retention_days: int
    audit_retention_days: int
    max_request_body_bytes: int
    rate_limit_requests_per_minute: int
    min_free_disk_mb: int
    customer_test_enabled: bool = False
    model_allow_coding_plan: bool = False
    max_react_steps: int = 4
    taobao_enabled: bool = False
    taobao_auto_reply_enabled: bool = False
    taobao_app_key: str = ""
    taobao_app_secret: str = ""
    taobao_redirect_uri: str = ""
    taobao_credential_key: str = ""
    taobao_qimen_customer_id: str = ""
    taobao_qimen_route_verified: bool = False
    taobao_chatrobot_request_token: str = ""
    taobao_chatrobot_tenant_id: str = ""
    taobao_top_gateway: str = "https://eco.taobao.com/router/rest"
    taobao_oauth_authorize_url: str = "https://oauth.taobao.com/authorize"
    taobao_oauth_token_url: str = "https://oauth.taobao.com/token"
    taobao_callback_max_skew_seconds: int = 600
    mockchat_enabled: bool = False
    mockchat_auto_reply_enabled: bool = False
    mockchat_secret: str = ""
    mockchat_callback_max_skew_seconds: int = 600
    mockchat_messages_per_minute: int = 120
    outbox_worker_enabled: bool = False
    outbox_sync_dispatch: bool = True
    outbox_poll_seconds: float = 1.0
    outbox_lease_seconds: int = 30
    outbox_batch_size: int = 20
    outbox_max_attempts: int = 5
    outbox_retry_base_seconds: int = 2
    outbox_retry_max_seconds: int = 300
    backup_dir: Path | None = None
    backup_key_id: str = "local-v1"
    backup_encryption_key: str = ""
    release_gate_required: bool = True
    channel_agent_worker_enabled: bool = False
    channel_agent_poll_seconds: float = 1.0
    channel_agent_lease_seconds: int = 300
    channel_agent_batch_size: int = 10
    channel_agent_max_attempts: int = 5
    channel_agent_retry_base_seconds: int = 2
    channel_agent_retry_max_seconds: int = 300
    competitive_monitor_worker_enabled: bool = False
    competitive_monitor_poll_seconds: float = 60.0
    handoff_sla_worker_enabled: bool = False
    handoff_sla_poll_seconds: float = 30.0
    handoff_dispatch_worker_enabled: bool = False
    handoff_dispatch_poll_seconds: float = 2.0
    handoff_dispatch_lease_seconds: int = 30
    handoff_dispatch_batch_size: int = 20
    handoff_dispatch_max_attempts: int = 5
    handoff_dispatch_retry_base_seconds: int = 5
    handoff_dispatch_retry_max_seconds: int = 300

    @property
    def app_db_path(self) -> Path:
        return self.data_dir / "agent.sqlite3"

    @property
    def checkpoint_db_path(self) -> Path:
        return self.data_dir / "checkpoints.sqlite3"

    @property
    def resolved_backup_dir(self) -> Path:
        return (self.backup_dir or (self.data_dir.parent / "backups")).resolve()

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "./data")).resolve(),
            model_provider=os.getenv("MODEL_PROVIDER", "glm").strip().lower(),
            model_base_url=os.getenv(
                "MODEL_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"
            ).rstrip("/"),
            model_name=os.getenv("MODEL_NAME", "glm-4.7-flash"),
            model_api_key=os.getenv("MODEL_API_KEY", "").strip(),
            model_timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "45")),
            model_max_output_tokens=int(os.getenv("MODEL_MAX_OUTPUT_TOKENS", "240")),
            model_temperature=float(os.getenv("MODEL_TEMPERATURE", "0.2")),
            model_thinking_enabled=_as_bool(os.getenv("MODEL_THINKING_ENABLED")),
            model_streaming=_as_bool(os.getenv("MODEL_STREAMING"), default=True),
            model_retry_attempts=max(0, min(2, int(os.getenv("MODEL_RETRY_ATTEMPTS", "1")))),
            model_enabled=_as_bool(os.getenv("MODEL_ENABLED"), default=False),
            model_mock_mode=_as_bool(os.getenv("MODEL_MOCK_MODE")),
            model_context_limit_tokens=int(
                os.getenv("MODEL_CONTEXT_LIMIT_TOKENS", "128000")
            ),
            context_budget_ratio=max(
                0.1, min(0.9, float(os.getenv("CONTEXT_BUDGET_RATIO", "0.7")))
            ),
            rag_top_k=int(os.getenv("RAG_TOP_K", "3")),
            rag_min_score=float(os.getenv("RAG_MIN_SCORE", "0.12")),
            rag_direct_approved_answer=_as_bool(
                os.getenv("RAG_DIRECT_APPROVED_ANSWER"), default=True
            ),
            rag_direct_approved_min_score=float(
                os.getenv("RAG_DIRECT_APPROVED_MIN_SCORE", "0.6")
            ),
            max_input_chars=int(os.getenv("MAX_INPUT_CHARS", "2000")),
            session_history_limit=int(os.getenv("SESSION_HISTORY_LIMIT", "6")),
            admin_api_key=os.getenv("ADMIN_API_KEY", ""),
            admin_auth_required=_as_bool(
                os.getenv("ADMIN_AUTH_REQUIRED"), default=True
            ),
            bootstrap_admin_id=os.getenv("BOOTSTRAP_ADMIN_ID", "local-admin"),
            auth_required=_as_bool(os.getenv("AUTH_REQUIRED"), default=True),
            bootstrap_tenant_id=os.getenv("BOOTSTRAP_TENANT_ID", "local-appliance"),
            bootstrap_client_id=os.getenv("BOOTSTRAP_CLIENT_ID", "local-adapter"),
            bootstrap_client_key=os.getenv("BOOTSTRAP_CLIENT_KEY", ""),
            bootstrap_client_can_supply_order_context=_as_bool(
                os.getenv("BOOTSTRAP_CLIENT_CAN_SUPPLY_ORDER_CONTEXT"), default=False
            ),
            subject_hash_key=os.getenv("SUBJECT_HASH_KEY", ""),
            session_idle_timeout_minutes=max(
                1, int(os.getenv("SESSION_IDLE_TIMEOUT_MINUTES", "120"))
            ),
            message_retention_days=max(1, int(os.getenv("MESSAGE_RETENTION_DAYS", "30"))),
            audit_retention_days=max(1, int(os.getenv("AUDIT_RETENTION_DAYS", "365"))),
            max_request_body_bytes=max(1024, int(os.getenv("MAX_REQUEST_BODY_BYTES", "16384"))),
            rate_limit_requests_per_minute=max(
                1, int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "120"))
            ),
            min_free_disk_mb=max(1, int(os.getenv("MIN_FREE_DISK_MB", "1024"))),
            customer_test_enabled=_as_bool(
                os.getenv("CUSTOMER_TEST_ENABLED"), default=False
            ),
            model_allow_coding_plan=_as_bool(
                os.getenv("MODEL_ALLOW_CODING_PLAN"), default=False
            ),
            max_react_steps=max(1, min(8, int(os.getenv("MAX_REACT_STEPS", "4")))),
            taobao_enabled=_as_bool(os.getenv("TAOBAO_ENABLED")),
            taobao_auto_reply_enabled=_as_bool(os.getenv("TAOBAO_AUTO_REPLY_ENABLED")),
            taobao_app_key=os.getenv("TAOBAO_APP_KEY", "").strip(),
            taobao_app_secret=os.getenv("TAOBAO_APP_SECRET", "").strip(),
            taobao_redirect_uri=os.getenv("TAOBAO_REDIRECT_URI", "").strip(),
            taobao_credential_key=os.getenv("TAOBAO_CREDENTIAL_KEY", "").strip(),
            taobao_qimen_customer_id=os.getenv("TAOBAO_QIMEN_CUSTOMER_ID", "").strip(),
            taobao_qimen_route_verified=_as_bool(
                os.getenv("TAOBAO_QIMEN_ROUTE_VERIFIED")
            ),
            taobao_chatrobot_request_token=os.getenv(
                "TAOBAO_CHATROBOT_REQUEST_TOKEN", ""
            ).strip(),
            taobao_chatrobot_tenant_id=os.getenv("TAOBAO_CHATROBOT_TENANT_ID", "").strip(),
            taobao_top_gateway=os.getenv(
                "TAOBAO_TOP_GATEWAY", "https://eco.taobao.com/router/rest"
            ).rstrip("/"),
            taobao_oauth_authorize_url=os.getenv(
                "TAOBAO_OAUTH_AUTHORIZE_URL", "https://oauth.taobao.com/authorize"
            ),
            taobao_oauth_token_url=os.getenv(
                "TAOBAO_OAUTH_TOKEN_URL", "https://oauth.taobao.com/token"
            ),
            taobao_callback_max_skew_seconds=max(
                60, int(os.getenv("TAOBAO_CALLBACK_MAX_SKEW_SECONDS", "600"))
            ),
            mockchat_enabled=_as_bool(os.getenv("MOCKCHAT_ENABLED")),
            mockchat_auto_reply_enabled=_as_bool(os.getenv("MOCKCHAT_AUTO_REPLY_ENABLED")),
            mockchat_secret=os.getenv("MOCKCHAT_SECRET", "").strip(),
            mockchat_callback_max_skew_seconds=max(
                60, int(os.getenv("MOCKCHAT_CALLBACK_MAX_SKEW_SECONDS", "600"))
            ),
            mockchat_messages_per_minute=max(
                1, int(os.getenv("MOCKCHAT_MESSAGES_PER_MINUTE", "120"))
            ),
            outbox_worker_enabled=_as_bool(
                os.getenv("OUTBOX_WORKER_ENABLED"), default=True
            ),
            outbox_sync_dispatch=_as_bool(os.getenv("OUTBOX_SYNC_DISPATCH"), default=False),
            outbox_poll_seconds=max(0.05, float(os.getenv("OUTBOX_POLL_SECONDS", "1"))),
            outbox_lease_seconds=max(30, int(os.getenv("OUTBOX_LEASE_SECONDS", "30"))),
            outbox_batch_size=max(1, min(100, int(os.getenv("OUTBOX_BATCH_SIZE", "20")))),
            outbox_max_attempts=max(1, min(20, int(os.getenv("OUTBOX_MAX_ATTEMPTS", "5")))),
            outbox_retry_base_seconds=max(
                1, int(os.getenv("OUTBOX_RETRY_BASE_SECONDS", "2"))
            ),
            outbox_retry_max_seconds=max(
                1, int(os.getenv("OUTBOX_RETRY_MAX_SECONDS", "300"))
            ),
            backup_dir=(
                Path(os.environ["BACKUP_DIR"]).resolve()
                if os.getenv("BACKUP_DIR", "").strip()
                else None
            ),
            backup_key_id=os.getenv("BACKUP_KEY_ID", "local-v1").strip(),
            backup_encryption_key=os.getenv("BACKUP_ENCRYPTION_KEY", "").strip(),
            release_gate_required=_as_bool(
                os.getenv("RELEASE_GATE_REQUIRED"), default=True
            ),
            channel_agent_worker_enabled=_as_bool(
                os.getenv("CHANNEL_AGENT_WORKER_ENABLED"), default=True
            ),
            channel_agent_poll_seconds=max(
                0.05, float(os.getenv("CHANNEL_AGENT_POLL_SECONDS", "1"))
            ),
            channel_agent_lease_seconds=max(
                30, int(os.getenv("CHANNEL_AGENT_LEASE_SECONDS", "300"))
            ),
            channel_agent_batch_size=max(
                1, min(100, int(os.getenv("CHANNEL_AGENT_BATCH_SIZE", "10")))
            ),
            channel_agent_max_attempts=max(
                1, min(20, int(os.getenv("CHANNEL_AGENT_MAX_ATTEMPTS", "5")))
            ),
            channel_agent_retry_base_seconds=max(
                1, int(os.getenv("CHANNEL_AGENT_RETRY_BASE_SECONDS", "2"))
            ),
            channel_agent_retry_max_seconds=max(
                1, int(os.getenv("CHANNEL_AGENT_RETRY_MAX_SECONDS", "300"))
            ),
            competitive_monitor_worker_enabled=_as_bool(
                os.getenv("COMPETITIVE_MONITOR_WORKER_ENABLED"), default=True
            ),
            competitive_monitor_poll_seconds=max(
                1.0, float(os.getenv("COMPETITIVE_MONITOR_POLL_SECONDS", "60"))
            ),
            handoff_sla_worker_enabled=_as_bool(
                os.getenv("HANDOFF_SLA_WORKER_ENABLED"), default=True
            ),
            handoff_sla_poll_seconds=max(
                5.0, float(os.getenv("HANDOFF_SLA_POLL_SECONDS", "30"))
            ),
            handoff_dispatch_worker_enabled=_as_bool(
                os.getenv("HANDOFF_DISPATCH_WORKER_ENABLED"), default=True
            ),
            handoff_dispatch_poll_seconds=max(
                0.1, float(os.getenv("HANDOFF_DISPATCH_POLL_SECONDS", "2"))
            ),
            handoff_dispatch_lease_seconds=max(
                10, int(os.getenv("HANDOFF_DISPATCH_LEASE_SECONDS", "30"))
            ),
            handoff_dispatch_batch_size=max(
                1, min(100, int(os.getenv("HANDOFF_DISPATCH_BATCH_SIZE", "20")))
            ),
            handoff_dispatch_max_attempts=max(
                1, min(20, int(os.getenv("HANDOFF_DISPATCH_MAX_ATTEMPTS", "5")))
            ),
            handoff_dispatch_retry_base_seconds=max(
                1, int(os.getenv("HANDOFF_DISPATCH_RETRY_BASE_SECONDS", "5"))
            ),
            handoff_dispatch_retry_max_seconds=max(
                1, int(os.getenv("HANDOFF_DISPATCH_RETRY_MAX_SECONDS", "300"))
            ),
        )

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
