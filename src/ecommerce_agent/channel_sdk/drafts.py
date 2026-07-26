from __future__ import annotations

import difflib
import json
import uuid
from typing import Any, Mapping

from ..database import Database, utc_now
from ..text_utils import redact_sensitive
from .contracts import ChannelAdapterError, ReplyDraftCommand


def reply_diff(before: str, after: str) -> list[dict[str, str]]:
    changes = []
    for operation, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, before, after
    ).get_opcodes():
        if operation == "equal":
            continue
        changes.append(
            {"operation": operation, "before": before[i1:i2], "after": after[j1:j2]}
        )
    return changes


def draft_view(row: Mapping[str, Any]) -> dict[str, Any]:
    view = dict(row)
    view["diff"] = json.loads(view.pop("diff_json") or "[]")
    view["evidence_ids"] = json.loads(view.pop("evidence_json") or "[]")
    view["sop_reference"] = json.loads(view.pop("sop_reference_json") or "null")
    return view


def create_reply_draft(
    db: Database, *, platform: str, command: ReplyDraftCommand
) -> tuple[dict[str, Any], bool]:
    """Create the durable reply draft an operator reviews before a manual send.

    Returns the draft view and whether it was newly created; a repeated
    idempotency key returns the stored draft unchanged.
    """
    with db.connect() as conn:
        conversation = conn.execute(
            "SELECT * FROM channel_conversations WHERE id=? AND tenant_id=? AND platform=?",
            (command.conversation_id, command.tenant_id, platform),
        ).fetchone()
        existing = conn.execute(
            "SELECT * FROM channel_reply_drafts WHERE tenant_id=? AND idempotency_key=?",
            (command.tenant_id, command.idempotency_key),
        ).fetchone()
        if command.source_event_id:
            inbound = conn.execute(
                """
                SELECT id FROM channel_events
                WHERE id=? AND tenant_id=? AND conversation_id=? AND direction='inbound'
                """,
                (command.source_event_id, command.tenant_id, command.conversation_id),
            ).fetchone()
        else:
            inbound = conn.execute(
                "SELECT id FROM channel_events WHERE conversation_id=? AND direction='inbound' "
                "ORDER BY created_at DESC LIMIT 1",
                (command.conversation_id,),
            ).fetchone()
    if existing is not None:
        if existing["conversation_id"] != command.conversation_id:
            raise ChannelAdapterError(
                "reply draft idempotency key belongs to another conversation",
                kind="conflict",
                platform=platform,
            )
        return draft_view(existing), False
    if conversation is None or inbound is None:
        raise ChannelAdapterError(
            "channel conversation or inbound event not found",
            kind="not_found",
            platform=platform,
        )
    if conversation["version"] != command.expected_conversation_version:
        raise ChannelAdapterError(
            "channel conversation version conflict", kind="conflict", platform=platform
        )
    if conversation["owner_mode"] != "human":
        raise ChannelAdapterError(
            "reply draft requires the conversation to be owned by a human",
            kind="conflict",
            platform=platform,
        )
    suggestion, _ = redact_sensitive(command.ai_suggestion)
    final_text, _ = redact_sensitive(command.final_text or command.ai_suggestion)
    draft_id = f"draft-{uuid.uuid4().hex}"
    now = utc_now()
    sop_reference = (
        {"id": command.sop_id, "version": command.sop_version}
        if command.sop_id and command.sop_version
        else None
    )
    with db._write_lock, db.connect() as conn:
        conn.execute(
            """
            INSERT INTO channel_reply_drafts(
                id, tenant_id, conversation_id, source_event_id,
                ai_suggestion_redacted, final_text_redacted, diff_json,
                evidence_json, sop_reference_json, confidence, risk_level,
                status, idempotency_key, outbox_id, last_error, record_version,
                created_by, sent_by, created_at, updated_at, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, NULL, NULL,
                      1, ?, NULL, ?, ?, NULL)
            """,
            (
                draft_id,
                command.tenant_id,
                command.conversation_id,
                inbound["id"],
                suggestion,
                final_text,
                json.dumps(reply_diff(suggestion, final_text), ensure_ascii=False),
                json.dumps(command.evidence_ids, ensure_ascii=False),
                json.dumps(sop_reference, ensure_ascii=False) if sop_reference else None,
                command.confidence,
                command.risk_level,
                command.idempotency_key,
                command.actor,
                now,
                now,
            ),
        )
        saved = conn.execute(
            "SELECT * FROM channel_reply_drafts WHERE id=?", (draft_id,)
        ).fetchone()
    return draft_view(saved), True
