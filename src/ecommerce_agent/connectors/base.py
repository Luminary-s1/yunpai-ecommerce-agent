from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


ConnectorMode = Literal["read", "write", "webhook", "polling", "bulk_export"]
ExternalStatus = Literal["accepted", "succeeded", "failed", "uncertain"]


class ConnectorCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    display_name: str
    capability_version: str
    virtual: bool = False
    resources: list[str]
    modes: list[ConnectorMode]
    actions: list[str] = Field(default_factory=list)
    supports_dry_run: bool = False
    supports_idempotency: bool = False
    supports_readback: bool = False
    data_classification: str = "business"
    required_permissions: list[str] = Field(default_factory=list)


class ConnectionCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    connector_id: str
    mode: Literal["virtual", "live"]
    detail: str


class PullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str = Field(min_length=1, max_length=64)
    cursor: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=100, ge=1, le=500)


class PullRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_version: str
    occurred_at: str
    payload: dict[str, Any]


class PullBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str
    resource: str
    records: list[PullRecord]
    next_cursor: str | None = None
    has_more: bool = False
    data_as_of: str


class VerifiedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool
    event_id: str
    event_type: str
    resource: str
    payload: dict[str, Any]


class ExternalAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = True


class ExternalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ExternalStatus
    external_request_id: str
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool
    status: ExternalStatus
    detail: str


@runtime_checkable
class Connector(Protocol):
    def capabilities(self) -> ConnectorCapabilities: ...

    def test_connection(self) -> ConnectionCheck: ...

    def pull(self, request: PullRequest) -> PullBatch: ...

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> VerifiedEvent: ...

    def execute(self, action: ExternalAction) -> ExternalResult: ...

    def verify(self, action: ExternalAction, result: ExternalResult) -> VerificationResult: ...

