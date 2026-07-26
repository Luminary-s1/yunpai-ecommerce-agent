from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from .auth import Principal
from .channel_sdk import (
    FAILURE_DELIVERY_STATES,
    ChannelAdapter,
    ChannelAdapterError,
    ChannelAdapterRegistry,
    OwnershipCommand,
    ReplyDraftCommand,
    SendCommand,
)
from .config import Settings
from .database import Database, utc_now
from .handoff import HandoffService
from .releases import ReleaseService
from .schemas import ChatResponse


class ChannelAgentError(RuntimeError):
    pass


class ChannelAgentBlocked(ChannelAgentError):
    pass


class ChannelAgentRuntime:
    """Processes durable channel Agent jobs against any registered adapter.

    The runtime never talks to a platform integration directly: each job is
    routed to the adapter declared for its platform, using only the channel
    SDK contracts.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        releases: ReleaseService,
        handoffs: HandoffService,
        adapters: ChannelAdapterRegistry,
        chat: Callable[..., ChatResponse],
    ) -> None:
        self.db = db
        self.settings = settings
        self.releases = releases
        self.handoffs = handoffs
        self.adapters = adapters
        self.chat = chat
        self._worker_thread: threading.Thread | None = None
        self._worker_stop = threading.Event()
        self._worker_lock = threading.Lock()
        self._worker_last_error: str | None = None
        self._worker_processed = 0

    def recover_expired_leases(self, *, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).isoformat()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE channel_agent_jobs
                SET status='retry', next_attempt_at=?, lease_owner=NULL,
                    lease_until=NULL, error_kind='worker_lost',
                    last_error='channel Agent worker lease expired', updated_at=?,
                    record_version=record_version+1
                WHERE status='running' AND lease_until IS NOT NULL AND lease_until<=?
                """,
                (current, current, current),
            )
        return int(cursor.rowcount)

    def claim_due(
        self,
        worker_id: str,
        *,
        limit: int,
        job_id: str | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        current_dt = now or datetime.now(UTC)
        current = current_dt.isoformat()
        lease_until = (
            current_dt + timedelta(seconds=self.settings.channel_agent_lease_seconds)
        ).isoformat()
        claimed: list[dict[str, Any]] = []
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            query = (
                "SELECT * FROM channel_agent_jobs "
                "WHERE status IN ('queued','retry') "
                "AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                "AND (lease_until IS NULL OR lease_until<=?)"
            )
            params: list[Any] = [current, current]
            if job_id is not None:
                query += " AND id=?"
                params.append(job_id)
            query += " ORDER BY created_at, id LIMIT ?"
            params.append(max(1, limit))
            rows = conn.execute(query, tuple(params)).fetchall()
            for row in rows:
                cursor = conn.execute(
                    """
                    UPDATE channel_agent_jobs
                    SET status='running', attempt_count=attempt_count+1,
                        lease_owner=?, lease_until=?, started_at=COALESCE(started_at, ?),
                        last_error=NULL, error_kind=NULL, updated_at=?,
                        record_version=record_version+1
                    WHERE id=? AND status IN ('queued','retry') AND record_version=?
                    """,
                    (
                        worker_id,
                        lease_until,
                        current,
                        current,
                        row["id"],
                        row["record_version"],
                    ),
                )
                if cursor.rowcount == 1:
                    saved = conn.execute(
                        "SELECT * FROM channel_agent_jobs WHERE id=?", (row["id"],)
                    ).fetchone()
                    claimed.append(dict(saved))
        return claimed

    def run_job_once(self, job_id: str) -> dict[str, Any]:
        report = self.run_once(
            worker_id=f"callback-{uuid.uuid4().hex}", limit=1, job_id=job_id
        )
        return report["items"][0] if report["items"] else self.get_job(job_id)

    def run_once(
        self,
        *,
        worker_id: str,
        limit: int | None = None,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        recovered = self.recover_expired_leases()
        claimed = self.claim_due(
            worker_id,
            limit=limit or self.settings.channel_agent_batch_size,
            job_id=job_id,
        )
        items: list[dict[str, Any]] = []
        for item in claimed:
            try:
                items.append(self._process(item, worker_id))
            except ChannelAgentBlocked as exc:
                items.append(
                    self._finish(
                        str(item["id"]),
                        worker_id,
                        status="blocked",
                        action="blocked",
                        error_kind="safety_gate",
                        last_error=str(exc),
                    )
                )
            except Exception as exc:
                items.append(self._mark_failure(item, worker_id, exc))
        return {"recovered": recovered, "claimed": len(claimed), "items": items}

    def _process(self, job: dict[str, Any], worker_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT j.*, e.content_redacted, e.direction, e.status AS event_status,
                       c.owner_mode, c.version AS conversation_version, c.buyer_hash
                FROM channel_agent_jobs j
                JOIN channel_events e ON e.id=j.event_id
                JOIN channel_conversations c ON c.id=j.conversation_id
                WHERE j.id=? AND j.tenant_id=e.tenant_id AND j.tenant_id=c.tenant_id
                """,
                (job["id"],),
            ).fetchone()
        if row is None or row["direction"] != "inbound":
            raise ChannelAgentError("channel Agent job has no valid inbound event")
        current = dict(row)
        tenant_id = str(current["tenant_id"])
        event_id = str(current["event_id"])
        conversation_id = str(current["conversation_id"])
        platform = str(current["platform"])
        adapter = self.adapters.get(platform)

        if not adapter.automation_enabled():
            return self._finish(
                str(job["id"]),
                worker_id,
                status="blocked",
                action="disabled",
                error_kind="automation_disabled",
                last_error="channel Agent automation is disabled",
            )
        if current["owner_mode"] != "bot":
            raise ChannelAgentBlocked(
                f"conversation owner changed to {current['owner_mode']} before Agent execution"
            )

        assignment = self.releases.assignment(
            tenant_id,
            str(current["platform"]),
            str(current["shop_id"]),
            conversation_id,
        )
        policy = assignment.get("policy")
        if self.settings.release_gate_required and (
            not policy or not assignment.get("selected")
        ):
            self.db.audit(
                "release.assignment.blocked",
                "release-gate",
                event_id,
                {
                    "conversation_id": conversation_id,
                    "reason": assignment["reason"],
                    "release_id": policy.get("id") if policy else None,
                },
                tenant_id,
            )
            return self._finish(
                str(job["id"]),
                worker_id,
                status="blocked",
                action="control",
                release_id=str(policy["id"]) if policy else None,
                release_mode=str(policy["mode"]) if policy else None,
                assignment_bucket=assignment.get("bucket"),
                error_kind=str(assignment["reason"]),
                last_error="release gate did not select this event",
            )

        release_id = str(policy["id"]) if policy else None
        release_mode = str(policy["mode"]) if policy else "automatic"
        self._set_stage(
            str(job["id"]),
            worker_id,
            "agent",
            release_id=release_id,
            release_mode=release_mode,
            assignment_bucket=assignment.get("bucket"),
        )
        principal = Principal(
            tenant_id=tenant_id,
            client_id=self.settings.bootstrap_client_id,
            subject_hash=str(current["buyer_hash"]),
            can_supply_order_context=False,
        )
        invocation_key = f"channel-event:{event_id}"
        answer = self.chat(
            principal,
            f"{platform}:{conversation_id}",
            str(current["content_redacted"]),
            {"platform": platform, "shop_id": str(current["shop_id"])},
            idempotency_key=invocation_key,
            execution_mode="shadow" if release_mode == "shadow" else "live",
            source_type="channel",
            source_reference=str(current["platform"]),
        )
        with self.db.connect() as conn:
            invocation = conn.execute(
                """
                SELECT id FROM agent_invocations
                WHERE tenant_id=? AND client_id=? AND idempotency_key=?
                """,
                (tenant_id, principal.client_id, invocation_key),
            ).fetchone()
        if invocation is None:
            raise ChannelAgentError("durable Agent invocation cannot be found")
        self._set_stage(
            str(job["id"]),
            worker_id,
            "agent_completed",
            agent_invocation_id=str(invocation["id"]),
            assistant_message_id=answer.message_id,
            context_snapshot_id=answer.context_snapshot_id,
        )

        observation: dict[str, Any] | None = None
        action = "handoff" if answer.requires_human else "send"
        if policy:
            observation = self.releases.record_response(
                tenant_id,
                assignment,
                conversation_id=conversation_id,
                event_id=event_id,
                response=answer,
            )
            action = str(observation["action"])
        self._set_stage(
            str(job["id"]),
            worker_id,
            "materialize",
            action=action,
            release_observation_id=(
                str(observation["id"]) if observation is not None else None
            ),
        )

        if action in {"control", "shadow"}:
            return self._finish(
                str(job["id"]),
                worker_id,
                status="completed",
                action=action,
            )
        if action in {"handoff", "draft"}:
            conversation = self._ensure_human_ownership(
                adapter,
                conversation_id,
                tenant_id,
                "agent-handoff" if action == "handoff" else "agent-assist",
            )
            handoff_id = answer.handoff_id
            if action == "handoff" and handoff_id is None:
                handoff = self.handoffs.create(
                    tenant_id=tenant_id,
                    session_id=str(
                        self._invocation_session(str(invocation["id"]), tenant_id)
                    ),
                    message_id=answer.message_id,
                    reason="release_policy_handoff",
                    payload={
                        "event_id": event_id,
                        "release_id": release_id,
                        "context_snapshot_id": answer.context_snapshot_id,
                    },
                )
                handoff_id = handoff.id
            draft_id: str | None = None
            if action == "draft" and not answer.requires_human:
                draft = adapter.create_reply_draft(
                    ReplyDraftCommand(
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        expected_conversation_version=int(conversation["version"]),
                        ai_suggestion=answer.answer,
                        evidence_ids=[source.id for source in answer.sources][:20],
                        sop_id=answer.sop_id,
                        sop_version=answer.sop_version,
                        risk_level=answer.risk_level,
                        idempotency_key=f"agent:{event_id}",
                        source_event_id=event_id,
                        actor="channel-agent",
                    )
                )
                draft_id = str(draft["id"])
            return self._finish(
                str(job["id"]),
                worker_id,
                status="completed",
                action=action,
                reply_draft_id=draft_id,
                last_error=(f"handoff:{handoff_id}" if handoff_id else None),
            )
        if action != "send":
            raise ChannelAgentBlocked(f"unsupported release action: {action}")
        if release_id:
            latest_policy = self.releases.get_policy(tenant_id, release_id)
            if latest_policy["status"] != "active":
                raise ChannelAgentBlocked(
                    f"release policy became {latest_policy['status']} before send"
                )
        try:
            receipt = adapter.send_reply(
                SendCommand(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    text=answer.answer,
                    idempotency_key=f"auto:{event_id}",
                    source_event_id=event_id,
                    actor="agent",
                    allow_bot=True,
                )
            )
        except Exception as exc:
            failure_kind = (
                exc.kind
                if isinstance(exc, ChannelAdapterError)
                else type(exc).__name__.lower()
            )
            if release_id and observation:
                self.releases.mark_delivery_failure(
                    tenant_id, release_id, event_id, failure_kind
                )
            self.db.audit(
                f"{platform}.auto_reply.failed",
                "agent",
                event_id,
                {"error_type": type(exc).__name__, "error": str(exc)[:300]},
                tenant_id,
            )
            raise
        delivery_state = receipt.delivery_state
        if release_id and delivery_state in FAILURE_DELIVERY_STATES:
            self.releases.mark_delivery_failure(
                tenant_id, release_id, event_id, delivery_state
            )
        return self._finish(
            str(job["id"]),
            worker_id,
            status="completed",
            action="send",
            outbox_id=receipt.outbox_id,
            error_kind=(delivery_state if delivery_state in FAILURE_DELIVERY_STATES else None),
            last_error=(
                f"delivery requires review: {delivery_state}"
                if delivery_state in FAILURE_DELIVERY_STATES
                else None
            ),
        )

    def _invocation_session(self, invocation_id: str, tenant_id: str) -> str:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM agent_invocations WHERE id=? AND tenant_id=?",
                (invocation_id, tenant_id),
            ).fetchone()
        if row is None:
            raise ChannelAgentError("Agent invocation session cannot be found")
        return str(row["session_id"])

    def _ensure_human_ownership(
        self,
        adapter: ChannelAdapter,
        conversation_id: str,
        tenant_id: str,
        assigned_to: str,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_conversations WHERE id=? AND tenant_id=?",
                (conversation_id, tenant_id),
            ).fetchone()
        if row is None:
            raise ChannelAgentBlocked("conversation disappeared before handoff")
        if row["owner_mode"] == "paused":
            raise ChannelAgentBlocked("conversation is paused")
        if row["owner_mode"] == "human":
            return dict(row)
        try:
            return adapter.change_ownership(
                OwnershipCommand(
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    owner_mode="human",
                    expected_version=int(row["version"]),
                    assigned_to=assigned_to,
                    actor="channel-agent",
                )
            )
        except ChannelAdapterError:
            with self.db.connect() as conn:
                latest = conn.execute(
                    "SELECT * FROM channel_conversations WHERE id=? AND tenant_id=?",
                    (conversation_id, tenant_id),
                ).fetchone()
            if latest is not None and latest["owner_mode"] == "human":
                return dict(latest)
            raise

    def _set_stage(
        self,
        job_id: str,
        worker_id: str,
        stage: str,
        **fields: Any,
    ) -> None:
        allowed = {
            "release_id",
            "release_mode",
            "assignment_bucket",
            "action",
            "agent_invocation_id",
            "assistant_message_id",
            "context_snapshot_id",
            "release_observation_id",
        }
        assignments = ["stage=?", "updated_at=?", "record_version=record_version+1"]
        params: list[Any] = [stage, utc_now()]
        for key, value in fields.items():
            if key not in allowed:
                raise ChannelAgentError(f"unsupported channel Agent job field: {key}")
            assignments.append(f"{key}=?")
            params.append(value)
        params.extend([job_id, worker_id])
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                f"UPDATE channel_agent_jobs SET {', '.join(assignments)} "
                "WHERE id=? AND status='running' AND lease_owner=?",
                tuple(params),
            )
        if cursor.rowcount != 1:
            raise ChannelAgentError("channel Agent job lease was lost")

    def _finish(
        self,
        job_id: str,
        worker_id: str,
        *,
        status: str,
        action: str,
        release_id: str | None = None,
        release_mode: str | None = None,
        assignment_bucket: int | None = None,
        reply_draft_id: str | None = None,
        outbox_id: str | None = None,
        error_kind: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE channel_agent_jobs
                SET status=?, stage='done', action=?,
                    release_id=COALESCE(?, release_id),
                    release_mode=COALESCE(?, release_mode),
                    assignment_bucket=COALESCE(?, assignment_bucket),
                    reply_draft_id=COALESCE(?, reply_draft_id),
                    outbox_id=COALESCE(?, outbox_id), error_kind=?, last_error=?,
                    lease_owner=NULL, lease_until=NULL, updated_at=?, completed_at=?,
                    record_version=record_version+1
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (
                    status,
                    action,
                    release_id,
                    release_mode,
                    assignment_bucket,
                    reply_draft_id,
                    outbox_id,
                    error_kind,
                    last_error,
                    now,
                    now,
                    job_id,
                    worker_id,
                ),
            )
            saved = conn.execute(
                "SELECT * FROM channel_agent_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if cursor.rowcount != 1 or saved is None:
            raise ChannelAgentError("channel Agent job completion lost its lease")
        self.db.audit(
            "channel.agent.completed" if status == "completed" else "channel.agent.blocked",
            worker_id,
            job_id,
            {"status": status, "action": action, "error_kind": error_kind},
            str(saved["tenant_id"]),
        )
        return self.public_view(dict(saved))

    def _mark_failure(
        self, job: dict[str, Any], worker_id: str, exc: Exception
    ) -> dict[str, Any]:
        attempt_count = int(job["attempt_count"])
        terminal = attempt_count >= int(job["max_attempts"])
        status = "dead_letter" if terminal else "retry"
        delay = min(
            self.settings.channel_agent_retry_max_seconds,
            self.settings.channel_agent_retry_base_seconds
            * (2 ** max(0, attempt_count - 1)),
        )
        next_attempt = (
            datetime.now(UTC) + timedelta(seconds=delay)
        ).isoformat()
        now = utc_now()
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
        with self.db._write_lock, self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE channel_agent_jobs
                SET status=?, next_attempt_at=?, lease_owner=NULL, lease_until=NULL,
                    error_kind=?, last_error=?, updated_at=?,
                    completed_at=CASE WHEN ?='dead_letter' THEN ? ELSE NULL END,
                    stage=CASE WHEN ?='dead_letter' THEN 'done' ELSE stage END,
                    record_version=record_version+1
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (
                    status,
                    next_attempt,
                    type(exc).__name__.lower(),
                    error,
                    now,
                    status,
                    now,
                    status,
                    job["id"],
                    worker_id,
                ),
            )
            saved = conn.execute(
                "SELECT * FROM channel_agent_jobs WHERE id=?", (job["id"],)
            ).fetchone()
        if cursor.rowcount != 1 or saved is None:
            raise ChannelAgentError("channel Agent failure recording lost its lease")
        self.db.audit(
            "channel.agent.dead_lettered" if terminal else "channel.agent.retry_scheduled",
            worker_id,
            str(job["id"]),
            {"attempt_count": attempt_count, "error_type": type(exc).__name__},
            str(saved["tenant_id"]),
        )
        return self.public_view(dict(saved))

    def get_job(self, job_id: str, tenant_id: str | None = None) -> dict[str, Any]:
        query = (
            "SELECT j.*, i.response_json FROM channel_agent_jobs j "
            "LEFT JOIN agent_invocations i ON i.id=j.agent_invocation_id WHERE j.id=?"
        )
        params: list[Any] = [job_id]
        if tenant_id is not None:
            query += " AND j.tenant_id=?"
            params.append(tenant_id)
        with self.db.connect() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        if row is None:
            raise ChannelAgentError("channel Agent job not found")
        return self.public_view(dict(row))

    def list_jobs(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["j.tenant_id=?"]
        params: list[Any] = [tenant_id]
        if status:
            clauses.append("j.status=?")
            params.append(status)
        if conversation_id:
            clauses.append("j.conversation_id=?")
            params.append(conversation_id)
        params.append(max(1, min(500, limit)))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT j.*, i.response_json FROM channel_agent_jobs j
                LEFT JOIN agent_invocations i ON i.id=j.agent_invocation_id
                WHERE {' AND '.join(clauses)}
                ORDER BY j.created_at DESC LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self.public_view(dict(row)) for row in rows]

    def summary(self, tenant_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS total FROM channel_agent_jobs
                WHERE tenant_id=? GROUP BY status
                """,
                (tenant_id,),
            ).fetchall()
            oldest = conn.execute(
                """
                SELECT created_at FROM channel_agent_jobs
                WHERE tenant_id=? AND status IN ('queued','retry')
                ORDER BY created_at LIMIT 1
                """,
                (tenant_id,),
            ).fetchone()
        return {
            "counts": {str(row["status"]): int(row["total"]) for row in rows},
            "oldest_pending_at": oldest["created_at"] if oldest else None,
            "worker": self.worker_status(),
        }

    def observe_delivery(self, outbox: dict[str, Any]) -> None:
        delivery_state = str(outbox.get("delivery_state") or "")
        if delivery_state not in {"rejected", "uncertain", "dead_letter"}:
            return
        tenant_id = str(outbox.get("tenant_id") or "")
        source_event_id = str(outbox.get("source_event_id") or "")
        if not tenant_id or not source_event_id:
            return
        with self.db.connect() as conn:
            job = conn.execute(
                """
                SELECT * FROM channel_agent_jobs
                WHERE tenant_id=? AND event_id=?
                """,
                (tenant_id, source_event_id),
            ).fetchone()
        if (
            job is None
            or not job["release_id"]
            or not job["release_observation_id"]
        ):
            return
        self.releases.mark_delivery_failure(
            tenant_id,
            str(job["release_id"]),
            source_event_id,
            delivery_state,
        )
        marker = f"delivery_{delivery_state}"
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conn.execute(
                """
                UPDATE channel_agent_jobs
                SET outbox_id=COALESCE(outbox_id, ?), error_kind=?, last_error=?,
                    updated_at=?, record_version=record_version+1
                WHERE id=? AND COALESCE(error_kind, '')<>?
                """,
                (
                    outbox.get("id"),
                    marker,
                    f"delivery requires review: {delivery_state}",
                    now,
                    job["id"],
                    marker,
                ),
            )

    @staticmethod
    def public_view(row: dict[str, Any]) -> dict[str, Any]:
        response_json = row.pop("response_json", None)
        row["agent_response"] = json.loads(response_json) if response_json else None
        return row

    def start_worker(self) -> None:
        if not self.settings.channel_agent_worker_enabled:
            return
        with self._worker_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._worker_stop.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="channel-agent-worker",
                daemon=True,
            )
            self._worker_thread.start()

    def stop_worker(self) -> None:
        with self._worker_lock:
            thread = self._worker_thread
            self._worker_stop.set()
        if thread is not None:
            thread.join(timeout=5)
        with self._worker_lock:
            if thread is None or not thread.is_alive():
                self._worker_thread = None

    def worker_status(self) -> dict[str, Any]:
        thread = self._worker_thread
        return {
            "enabled": self.settings.channel_agent_worker_enabled,
            "running": bool(thread and thread.is_alive()),
            "poll_seconds": self.settings.channel_agent_poll_seconds,
            "processed": self._worker_processed,
            "last_error": self._worker_last_error,
        }

    def _worker_loop(self) -> None:
        worker_id = f"channel-worker-{uuid.uuid4().hex}"
        while not self._worker_stop.is_set():
            try:
                report = self.run_once(worker_id=worker_id)
                self._worker_processed += len(report["items"])
                self._worker_last_error = None
            except Exception as exc:
                self._worker_last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                self.db.audit(
                    "channel.agent.worker_failed",
                    worker_id,
                    "scheduler",
                    {"error_type": type(exc).__name__},
                    self.settings.bootstrap_tenant_id,
                )
            self._worker_stop.wait(self.settings.channel_agent_poll_seconds)

    def close(self) -> None:
        self.stop_worker()
