from __future__ import annotations

from typing import Any

from ..database import Database, utc_now
from .contracts import ChannelAdapterError, OwnershipCommand


def assert_send_ownership(owner_mode: str, allow_bot: bool, platform: str) -> None:
    if allow_bot and owner_mode != "bot":
        raise ChannelAdapterError(
            "automatic reply requires the conversation to be owned by the bot",
            kind="conflict",
            platform=platform,
        )
    if not allow_bot and owner_mode != "human":
        raise ChannelAdapterError(
            "manual reply requires the conversation to be owned by a human",
            kind="conflict",
            platform=platform,
        )


def change_ownership(
    db: Database, *, platform: str, command: OwnershipCommand
) -> dict[str, Any]:
    assigned_to = command.assigned_to or (
        command.actor if command.owner_mode == "human" else None
    )
    with db._write_lock, db.connect() as conn:
        current = conn.execute(
            "SELECT * FROM channel_conversations WHERE id=? AND tenant_id=? AND platform=?",
            (command.conversation_id, command.tenant_id, platform),
        ).fetchone()
        if current is None:
            raise ChannelAdapterError(
                "channel conversation not found", kind="not_found", platform=platform
            )
        if current["owner_mode"] == command.owner_mode:
            raise ChannelAdapterError(
                "channel conversation is already in the requested owner mode",
                kind="conflict",
                platform=platform,
            )
        cursor = conn.execute(
            """
            UPDATE channel_conversations
            SET owner_mode=?, assigned_to=?, version=version+1, updated_at=?
            WHERE id=? AND tenant_id=? AND version=?
            """,
            (
                command.owner_mode,
                assigned_to,
                utc_now(),
                command.conversation_id,
                command.tenant_id,
                command.expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise ChannelAdapterError(
                "channel conversation version conflict", kind="conflict", platform=platform
            )
        updated = conn.execute(
            """
            SELECT id, shop_id, buyer_nick_masked, owner_mode, assigned_to, version,
                   last_message_at, created_at, updated_at
            FROM channel_conversations WHERE id=?
            """,
            (command.conversation_id,),
        ).fetchone()
    db.audit(
        f"{platform}.conversation.ownership_changed",
        command.actor,
        command.conversation_id,
        {"from": current["owner_mode"], "to": command.owner_mode},
        command.tenant_id,
    )
    return dict(updated)
