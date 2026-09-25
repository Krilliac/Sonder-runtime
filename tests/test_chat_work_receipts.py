"""Verified session provenance and truthful work outcome over the live seam."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.interfaces.http import serve


def _store(monkeypatch, tmp_path):
    database = tmp_path / "work-session.sqlite"
    repository = SQLiteSessionRepository(database)
    monkeypatch.setattr(
        bootstrap_app, "default_app",
        lambda: SimpleNamespace(session_repository=lambda: repository),
    )
    return database, repository


def _work(*, session_id, session_ref, context=None, idempotency_key=""):
    return serve._handle_work_intent(
        "  Build the Flutter app.  ", project="project-name", authorized=True,
        context=context or {"mode": "local-open"},
        session_id=session_id, session_ref=session_ref,
        correlation_id="correlation-1", idempotency_key=idempotency_key,
        with_receipt=True,
    )


def test_work_handoff_uses_verified_previous_chat_event_and_returns_durable_receipt(
    tmp_path, monkeypatch,
):
    database, repository = _store(monkeypatch, tmp_path)
    SessionCaptureService(repository).capture_turn(
        "session-owned", "turn-prior", ModelRequest("previous private question", "general"),
        request_id="request-prior", user_message="previous private question",
        model_response="previous private answer",
    )
    prior = repository.read_complete("session-owned")[-1]
    seen = []

    def routed(prompt, *, project, _admitted_decision, **kwargs):
        handoff = _admitted_decision.handoff
        seen.append((prompt, project, handoff))
        assert [event.event_type for event in repository.read_complete("session-owned")][-1] == "chat.work.admitted"
        return "bounded lane returned"

    monkeypatch.setattr(server, "route_work_request", routed)
    result = _work(session_id="session-owned", session_ref="session-owned")

    assert result.status == "returned"
    assert result.text == "bounded lane returned"
    assert seen[0][0] == "  Build the Flutter app.  "
    assert seen[0][1] == "project-name"
    assert seen[0][2].objective == seen[0][0]
    assert seen[0][2].durable_context_refs == ("session-event:" + prior.event_id,)
    assert seen[0][2].constraints == seen[0][2].success_criteria == ()
    events = SQLiteSessionRepository(database).read_complete("session-owned")
    admission, finished = events[-2:]
    assert admission.event_type == "chat.work.admitted"
    assert finished.event_type == "chat.work.returned"
    assert finished.payload["admission_event_id"] == admission.event_id
    assert result.public_receipt()["source_event_id"] == prior.event_id
    assert result.public_receipt()["return_event_id"] == finished.event_id
    assert result.public_receipt()["session_ref"] == "session-owned"
    assert "previous private" not in json.dumps([admission.payload, finished.payload])
    assert "Build the Flutter app" not in json.dumps([admission.payload, finished.payload])
    assert SessionCaptureService(SQLiteSessionRepository(database)).replay("session-owned").crash_safe


def test_unknown_lane_outcome_and_exception_preserve_uncertainty(tmp_path, monkeypatch):
    _database, repository = _store(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "route_work_request", lambda *args, **kwargs: None)
    result = _work(session_id="unknown", session_ref="unknown")
    assert result.status == "unknown"
    assert result.text == ""
    assert [e.event_type for e in repository.read_complete("unknown")] == [
        "chat.work.admitted", "chat.work.unknown",
    ]

    def failed(*args, **kwargs):
        raise RuntimeError("lane may have started")

    monkeypatch.setattr(server, "route_work_request", failed)
    with pytest.raises(RuntimeError, match="lane may have started"):
        _work(session_id="failed", session_ref="failed")
    assert [e.event_type for e in repository.read_complete("failed")] == [
        "chat.work.admitted", "chat.work.unknown",
    ]


def test_account_namespace_cannot_select_another_accounts_prior_chat(tmp_path, monkeypatch):
    _database, repository = _store(monkeypatch, tmp_path)
    alice = {"mode": "account", "account": {"username": "alice"}}
    bob = {"mode": "account", "account": {"username": "bob"}}
    alice_id = serve._hosted_storage_id(alice, "shared-name", "session")
    bob_id = serve._hosted_storage_id(bob, "shared-name", "session")
    assert alice_id != bob_id
    SessionCaptureService(repository).capture_turn(
        alice_id, "alice-turn", ModelRequest("private Alice prompt", "general"),
        request_id="alice-request", model_response="private Alice response",
    )
    seen = []
    monkeypatch.setattr(
        server, "route_work_request",
        lambda *args, **kwargs: seen.append(kwargs["_admitted_decision"].handoff) or "returned",
    )
    bob_result = _work(session_id=bob_id, session_ref="shared-name", context=bob)
    assert bob_result.status == "returned"
    assert "source_event_id" not in bob_result.public_receipt()
    assert seen[0].durable_context_refs == ()
    alice_result = _work(session_id=alice_id, session_ref="shared-name", context=alice)
    assert alice_result.public_receipt()["source_event_id"]
    assert len(seen[1].durable_context_refs) == 1


def test_replayed_idempotent_work_does_not_claim_a_second_lane_return(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence import served_action_receipts
    from sonder_runtime.adapters.web import lifecycle as sonder_lifecycle

    _database, repository = _store(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_SERVED_ACTION_RECEIPTS_DB", str(tmp_path / "actions.sqlite"))
    served_action_receipts.reset_for_tests()
    sonder_lifecycle.reset_for_tests()
    calls = []
    monkeypatch.setattr(
        server, "route_work_request",
        lambda *args, **kwargs: calls.append("lane") or "lane returned",
    )
    context = {"mode": "account", "account": {"username": "alice"}}
    try:
        first = _work(
            session_id="owned-id", session_ref="client-name", context=context,
            idempotency_key="one-action",
        )
        cached = _work(
            session_id="owned-id", session_ref="client-name", context=context,
            idempotency_key="one-action",
        )
        assert first == cached
        assert calls == ["lane"]
        assert len(repository.read_complete("owned-id")) == 2
        # The same key for another session is a different request: refused,
        # never run and never answered with the first session's receipt.
        reused = _work(
            session_id="other-owned-id", session_ref="other-client-name", context=context,
            idempotency_key="one-action",
        )
        assert reused.status == "refused"
        assert reused.text.startswith("idempotency key reused")
        assert "admission_event_id" not in reused.public_receipt()
        assert calls == ["lane"]
        other_session = _work(
            session_id="other-owned-id", session_ref="other-client-name", context=context,
            idempotency_key="another-action",
        )
        assert other_session.status == "returned"
        assert other_session.session_ref == "other-client-name"
        assert other_session.admission_event_id != first.admission_event_id
        assert calls == ["lane", "lane"]
        assert len(repository.read_complete("other-owned-id")) == 2

        sonder_lifecycle.reset_for_tests()
        after_restart = _work(
            session_id="owned-id", session_ref="client-name", context=context,
            idempotency_key="one-action",
        )
        assert after_restart.status == "refused"
        assert "return_event_id" not in after_restart.public_receipt()
        assert calls == ["lane", "lane"]
        assert len(repository.read_complete("owned-id")) == 2
    finally:
        sonder_lifecycle.reset_for_tests()
        served_action_receipts.reset_for_tests()
