from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ecommerce_agent.config import Settings
from ecommerce_agent.service import AgentService

from conftest import make_settings, principal_for


def test_session_idle_timeout_is_independent_from_message_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESSAGE_RETENTION_DAYS", "1")
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_MINUTES", "120")
    settings = Settings.from_env()

    assert settings.message_retention_days == 1
    assert settings.session_idle_timeout_minutes == 120


def test_idle_worker_closes_121_minute_session_but_skips_open_handoff(
    tmp_path,
) -> None:
    settings = replace(
        make_settings(tmp_path),
        session_idle_timeout_minutes=120,
    )
    service = AgentService(settings)
    service.SESSION_IDLE_WORKER_POLL_SECONDS = 0.05
    principal = principal_for(service)
    try:
        closable_id = service.db.resolve_session(
            tenant_id=principal.tenant_id,
            client_id=principal.client_id,
            external_session_id="idle-closable",
            subject_hash=principal.subject_hash,
        )
        protected = service.chat(principal, "idle-handoff", "转人工")
        assert protected.handoff_id
        cutoff = (datetime.now(UTC) - timedelta(minutes=121)).isoformat()
        with service.db._write_lock, service.db.connect() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen_at=? WHERE id=? OR external_session_id=?",
                (cutoff, closable_id, "idle-handoff"),
            )

        service.start_session_idle_worker()
        deadline = time.monotonic() + 3
        while (
            service.session_idle_worker_status()["cycles"] < 1
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        with service.db.connect() as conn:
            rows = {
                row["external_session_id"]: row["status"]
                for row in conn.execute(
                    """
                    SELECT external_session_id, status FROM sessions
                    WHERE external_session_id IN ('idle-closable','idle-handoff')
                    """
                ).fetchall()
            }
        assert rows == {
            "idle-closable": "closed",
            "idle-handoff": "active",
        }
        assert service.session_idle_worker_status()["closed"] == 1
    finally:
        service.close()
