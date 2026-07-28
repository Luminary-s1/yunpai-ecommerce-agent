from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..database import Database, utc_now
from .contracts import ChannelAdapterError, InboundEnvelope


@dataclass(frozen=True, slots=True)
class InboundRecord:
    conversation_id: str
    event_id: str
    is_new: bool
    owner_mode: str
    job_id: str | None


class ChannelInboundRecorder:
    """Durable inbound contract shared by every channel adapter.

    One transaction covers the conversation upsert, the exact-once event insert
    (dedup on tenant/platform/shop/external event) and the Agent job whose id is
    stable for the event, so redelivered payloads replay without side effects.
    """

    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        *,
        tenant_id: str,
        platform: str,
        shop_id: str,
        external_conversation_id: str,
        external_event_id: str,
        message_type: str,
        content_redacted: str,
        payload_hash: str,
        buyer_hash: str,
        buyer_nick_masked: str | None,
        routing_ciphertext: str | None,
        request_id: str | None,
        action_mode: str | None,
        default_owner_mode: str,
        job_max_attempts: int,
    ) -> InboundRecord:
        now = utc_now()
        with self.db._write_lock, self.db.connect() as conn:
            conversation = conn.execute(
                """
                SELECT * FROM channel_conversations
                WHERE tenant_id=? AND platform=? AND shop_id=? AND external_conversation_id=?
                """,
                (tenant_id, platform, shop_id, external_conversation_id),
            ).fetchone()
            if conversation is None:
                conversation_id = f"conversation-{uuid.uuid4().hex}"
                owner_mode = default_owner_mode
                conn.execute(
                    """
                    INSERT INTO channel_conversations(
                        id, tenant_id, platform, shop_id, external_conversation_id,
                        buyer_hash, buyer_nick_masked, owner_mode, assigned_to, version,
                        last_event_id, last_message_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        tenant_id,
                        platform,
                        shop_id,
                        external_conversation_id,
                        buyer_hash,
                        buyer_nick_masked,
                        owner_mode,
                        external_event_id,
                        now,
                        now,
                        now,
                    ),
                )
            else:
                conversation_id = str(conversation["id"])
                owner_mode = str(conversation["owner_mode"])
                conn.execute(
                    """
                    UPDATE channel_conversations
                    SET last_event_id=?, last_message_at=?, updated_at=? WHERE id=?
                    """,
                    (external_event_id, now, now, conversation_id),
                )
            event_id = f"event-{uuid.uuid4().hex}"
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO channel_events(
                    id, tenant_id, platform, shop_id, conversation_id, external_event_id,
                    direction, message_type, content_redacted, payload_hash, routing_ciphertext,
                    request_id, action_mode, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'inbound', ?, ?, ?, ?, ?, ?, 'received', ?, ?)
                """,
                (
                    event_id,
                    tenant_id,
                    platform,
                    shop_id,
                    conversation_id,
                    external_event_id,
                    message_type,
                    content_redacted,
                    payload_hash,
                    routing_ciphertext,
                    request_id,
                    action_mode,
                    now,
                    now,
                ),
            )
            is_new = cursor.rowcount == 1
            if not is_new:
                stored_event = conn.execute(
                    """
                    SELECT id FROM channel_events
                    WHERE tenant_id=? AND platform=? AND shop_id=?
                      AND external_event_id=? AND direction='inbound'
                    """,
                    (tenant_id, platform, shop_id, external_event_id),
                ).fetchone()
                if stored_event is not None:
                    event_id = str(stored_event["id"])
            job_id: str | None = None
            if is_new:
                stable = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"yunpai:{tenant_id}:{platform}:{event_id}",
                ).hex
                job_id = f"channel-job-{stable}"
                conn.execute(
                    """
                    INSERT INTO channel_agent_jobs(
                        id, tenant_id, platform, shop_id, conversation_id, event_id,
                        status, stage, release_id, release_mode, assignment_bucket,
                        action, agent_invocation_id, assistant_message_id,
                        context_snapshot_id, release_observation_id, reply_draft_id,
                        outbox_id, attempt_count, max_attempts, next_attempt_at,
                        lease_owner, lease_until, last_error, error_kind, record_version,
                        created_at, updated_at, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 'queued', NULL, NULL, NULL,
                              NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, ?, ?, NULL,
                              NULL, NULL, NULL, 1, ?, ?, NULL, NULL)
                    """,
                    (
                        job_id,
                        tenant_id,
                        platform,
                        shop_id,
                        conversation_id,
                        event_id,
                        job_max_attempts,
                        now,
                        now,
                        now,
                    ),
                )
        return InboundRecord(conversation_id, event_id, is_new, owner_mode, job_id)

    def load_envelope(
        self,
        *,
        tenant_id: str,
        event_id: str,
        is_duplicate: bool,
        agent_job_id: str | None,
    ) -> InboundEnvelope:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT e.id AS event_id, e.platform, e.shop_id, e.external_event_id,
                       e.message_type, e.content_redacted, e.payload_hash,
                       e.created_at AS received_at, c.id AS conversation_id,
                       c.external_conversation_id, c.buyer_hash, c.owner_mode
                FROM channel_events e
                JOIN channel_conversations c
                  ON c.id=e.conversation_id AND c.tenant_id=e.tenant_id
                WHERE e.id=? AND e.tenant_id=? AND e.direction='inbound'
                """,
                (event_id, tenant_id),
            ).fetchone()
        if row is None:
            raise ChannelAdapterError("inbound channel event not found", kind="not_found")
        return InboundEnvelope(
            platform=str(row["platform"]),
            tenant_id=tenant_id,
            shop_id=str(row["shop_id"]),
            conversation_id=str(row["conversation_id"]),
            external_conversation_id=str(row["external_conversation_id"]),
            buyer_hash=str(row["buyer_hash"]),
            owner_mode=str(row["owner_mode"]),
            event_id=str(row["event_id"]),
            external_event_id=str(row["external_event_id"]),
            message_type=str(row["message_type"]),
            content_redacted=str(row["content_redacted"]),
            payload_hash=str(row["payload_hash"]),
            received_at=str(row["received_at"]),
            is_duplicate=is_duplicate,
            agent_job_id=agent_job_id,
        )
