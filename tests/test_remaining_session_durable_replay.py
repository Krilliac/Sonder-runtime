from __future__ import annotations

from dataclasses import replace
import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.durable_replay import (
    crash_safe_replay, reconstruct_model_visible_request,
)
from sonder_runtime.application.ports.session_repository import IntegrityIssue, IntegrityReport
from sonder_runtime.domain.common.errors import IntegrityFailure
from sonder_runtime.domain.common.events import DomainEvent


def _snapshot(repo: SQLiteSessionRepository) -> None:
    repo.append("s1", "session.started", {"status": "active"})
    repo.append("s1", "model.requested", {
        "request_id": "r1", "turn_id": "t1", "prompt": "hello", "tier": "code",
        "system": "be precise", "history": [{"role": "user", "content": "old"}],
        "options": {"temperature": 0}, "stream": True,
        "tools": [{"name": "search", "description": "lookup"}],
        "ui_facts": {"surface": "chat", "visible_artifact": "a1"},
    })
    repo.append("s1", "user.message", {"content": "hello", "turn_id": "t1"})
    repo.append("s1", "model.response", {"content": "done", "turn_id": "t1"})


def test_reconstructs_complete_model_visible_request() -> None:
    events = (
        DomainEvent("model.requested", "session", "s", 1, {
            "request_id": "r", "turn_id": "t", "prompt": "p", "tier": "code",
            "history": [], "options": {}, "tools": [{"name": "x"}],
            "ui_facts": {"surface": "workbench"},
        }),
    )
    result = reconstruct_model_visible_request(events)
    assert result is not None
    assert result.request.prompt == "p"
    assert result.tools[0]["name"] == "x"
    assert result.ui_facts["surface"] == "workbench"
    assert len(result.snapshot_digest) == 64


def test_crash_safe_replay_verifies_chain_and_replays_request(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    _snapshot(repo)
    result = crash_safe_replay(repo, "s1")
    assert result.crash_safe
    assert result.recovered_sequence == 4
    assert result.integrity.valid
    assert result.request is not None
    assert result.request.tools[0]["name"] == "search"
    assert result.replay.transcript[-1].content == "done"


def test_replay_rejects_tampered_durable_event(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    _snapshot(repo)
    class CorruptIntegrityView:
        _max_read_limit = 100

        def read_range(self, *args, **kwargs):
            return repo.read_range(*args, **kwargs)

        def inspect_integrity(self, session_id, **kwargs):
            report = repo.inspect_integrity(session_id, **kwargs)
            return replace(report, valid=False, issues=(IntegrityIssue(2, "tampered", "test corruption"),))

    with pytest.raises(IntegrityFailure, match="integrity"):
        crash_safe_replay(CorruptIntegrityView(), "s1")


def test_replay_rejects_bounded_prefix_as_not_crash_safe(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    _snapshot(repo)
    with pytest.raises(IntegrityFailure, match="exceeds replay bound"):
        crash_safe_replay(repo, "s1", max_events=4)


def test_replay_rejects_integrity_report_for_a_different_snapshot(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    _snapshot(repo)

    class MismatchedIntegrityView:
        _max_read_limit = 100

        def read_range(self, *args, **kwargs):
            return repo.read_range(*args, **kwargs)[:3]

        def inspect_integrity(self, session_id, **kwargs):
            return repo.inspect_integrity(session_id, **kwargs)

    with pytest.raises(IntegrityFailure, match="does not match"):
        crash_safe_replay(MismatchedIntegrityView(), "s1")


def test_replay_rejects_integrity_report_for_a_shorter_snapshot(tmp_path) -> None:
    repo = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    _snapshot(repo)

    class StaleIntegrityView:
        _max_read_limit = 100

        def read_range(self, *args, **kwargs):
            return repo.read_range(*args, **kwargs)

        def inspect_integrity(self, session_id, **kwargs):
            report = repo.inspect_integrity(session_id, **kwargs)
            return replace(report, checked_events=3, last_sequence=3)

    with pytest.raises(IntegrityFailure, match="does not match"):
        crash_safe_replay(StaleIntegrityView(), "s1")


def test_snapshot_digest_detects_changed_request_facts() -> None:
    event = DomainEvent("model.requested", "session", "s", 1, {
        "request_id": "r", "turn_id": "t", "prompt": "p", "tier": "code",
        "history": [], "options": {}, "tools": [], "ui_facts": {},
        "snapshot_digest": "0" * 64,
    })
    with pytest.raises(IntegrityFailure, match="digest"):
        reconstruct_model_visible_request((event,))
