from __future__ import annotations

from dataclasses import replace

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.http_facade import HttpSessionFacade
from sonder_runtime.application.ports.session_repository import IntegrityIssue


def _repo(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    repo.append("s1", "session.started", {"status": "active"})
    repo.append("s1", "user.message", {"content": "hello"})
    repo.append("s1", "model.response", {"content": "token=super-secret"})
    return repo


def test_read_is_bounded_and_redacted(tmp_path):
    facade = HttpSessionFacade(_repo(tmp_path), max_page_size=2, max_scan=3)
    result = facade.read("s1", page_size=2)

    assert result.status_code == 200
    assert result.body["schema"] == "sonder.http-session-page.v1"
    assert len(result.body["records"]) == 2
    assert result.body["records"][1]["payload"]["content"] == "hello"


def test_export_is_privacy_safe_and_bounded(tmp_path):
    facade = HttpSessionFacade(_repo(tmp_path), max_page_size=3, max_scan=3)
    result = facade.export("s1", max_events=3)

    assert result.status_code == 200
    assert result.body["integrity_valid"] is True
    assert "super-secret" not in str(result.body)


def test_replay_verifies_chain_and_returns_redacted_projection(tmp_path):
    facade = HttpSessionFacade(_repo(tmp_path), max_replay_events=10)
    result = facade.replay("s1")

    assert result.status_code == 200
    assert result.body["crash_safe"] is True
    assert result.body["recovered_sequence"] == 3
    assert result.body["transcript"][-1]["content"] == "token=[REDACTED]"


def test_invalid_query_is_a_safe_client_error(tmp_path):
    facade = HttpSessionFacade(_repo(tmp_path))
    result = facade.read("s1", page_size=101)
    assert result.status_code == 400
    assert result.body == {"error": "invalid_session_query"}


def test_integrity_failure_does_not_leak_event_data(tmp_path):
    repo = _repo(tmp_path)

    class CorruptView:
        _max_read_limit = 100

        def read_range(self, *args, **kwargs):
            return repo.read_range(*args, **kwargs)

        def inspect_integrity(self, session_id, **kwargs):
            report = repo.inspect_integrity(session_id, **kwargs)
            return replace(report, valid=False,
                           issues=(IntegrityIssue(2, "tampered", "corrupt"),))

    result = HttpSessionFacade(CorruptView()).replay("s1")
    assert result.status_code == 409
    assert result.body == {"error": "session_replay_unavailable"}
    assert "super-secret" not in str(result.body)
