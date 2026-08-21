from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.session import SessionContinuityService
from sonder_runtime.application.session.http_facade import HttpSessionFacade
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.common.errors import IntegrityFailure
from sonder_runtime.domain.common.ids import SessionId
from sonder_runtime.interfaces.http.facades.session import dispatch_session_route


def _seed(repository, session_id: str) -> None:
    repository.append(session_id, "session.started", {"privacy_class": "public_metadata"})
    repository.append(session_id, "user.message", {"content": "keep this"})
    repository.append(session_id, "model.requested", {"request_id": "req-1", "prompt": "side effect"})


def test_application_graph_wires_continuity_and_checkpoint_survives_restart(tmp_path, monkeypatch):
    database = tmp_path / "sessions.db"
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(database))
    bootstrap_app.reset_for_tests()
    first = bootstrap_app.build_application()
    repository = first.session_repository()
    session_id = SessionId.new().serialize()
    _seed(repository, session_id)
    continuity = first.session_continuity_service()
    context = local_owner_context(correlation_id="continuity-checkpoint", source="system")

    checkpoint = continuity.checkpoint(session_id, context=context)
    assert checkpoint.source_sequence == 3
    assert continuity.load_checkpoint(session_id) == checkpoint
    assert first.session_continuity_service() is continuity
    assert first.session_http_facade()._continuity is continuity

    bootstrap_app.reset_for_tests()
    reopened = bootstrap_app.build_application()
    assert reopened.session_continuity_service().load_checkpoint(session_id) == checkpoint


def test_fork_repair_and_routes_are_bounded_and_fail_closed(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    session_id = SessionId.new().serialize()
    _seed(repository, session_id)
    service = SessionContinuityService(repository)

    fork = service.fork(session_id, 2)
    assert fork.lineage.parent_session_id.serialize() == session_id
    assert len(fork.inherited_events) == 2
    resume = service.resume(session_id)
    assert resume.diagnosis.disposition == "truncated"
    assert resume.resume_sequence == 3

    facade = HttpSessionFacade(repository)
    repair = dispatch_session_route(facade, f"/v1/sessions/{session_id}/repair")
    planned_fork = dispatch_session_route(
        facade, f"/v1/sessions/{session_id}/fork", query={"fork_sequence": ["2"]}
    )
    assert repair.status_code == 200 and repair.body["can_resume"]
    assert planned_fork.status_code == 200

    with repository._connect() as connection:
        connection.execute("DROP TRIGGER session_event_no_update")
        connection.execute("UPDATE session_event SET payload_json = replace(payload_json, 'keep this', 'tampered')")
    with pytest.raises(IntegrityFailure):
        service.resume(session_id)


def test_retention_execution_is_owner_gated_append_only_and_privacy_safe(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    session_id = SessionId.new().serialize()
    repository.append(session_id, "secret.event", {
        "privacy_class": "secret", "content": "do-not-export",
    }, occurred_at_utc="2020-01-01T00:00:00Z")
    service = SessionContinuityService(repository)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(PermissionError):
        service.execute_retention(session_id, now_utc=now)

    result = service.execute_retention(
        session_id, now_utc=now,
        context=local_owner_context(correlation_id="retention", source="system"),
    )
    assert len(result.applied) == 1
    assert result.marker is not None
    assert len(repository.read_range(session_id, limit=100)) == 2

    facade = HttpSessionFacade(repository)
    exported = facade.export(session_id, max_events=10)
    assert exported.status_code == 200
    assert "do-not-export" not in str(exported.body)
    again = service.execute_retention(
        session_id, now_utc=now,
        context=local_owner_context(correlation_id="retention-again", source="system"),
    )
    assert again.applied == ()
