from __future__ import annotations

from sonder_runtime.application.session.http_facade import HttpSessionFacade
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.interfaces.http import serve
from sonder_runtime.interfaces.http.facades.session import dispatch_session_route


def _facade(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    repository.append("s1", "session.started", {"status": "active"})
    repository.append("s1", "user.message", {"content": "hello"})
    repository.append("s1", "model.response", {"content": "world"})
    return HttpSessionFacade(repository, max_replay_events=1_000)


def test_typed_session_routes_dispatch_without_legacy_runtime(tmp_path):
    facade = _facade(tmp_path)

    events = dispatch_session_route(facade, "/v1/sessions/s1/events", query={"page_size": ["1"]})
    exported = dispatch_session_route(facade, "/v1/sessions/s1/export", query={"max_events": ["2"]})
    replay = dispatch_session_route(facade, "/v1/sessions/s1/replay")

    assert events.status_code == 200
    assert len(events.body["records"]) == 1
    assert exported.status_code == 200
    assert exported.body["session_id"] == "s1"
    assert replay.status_code == 200
    assert replay.body["integrity_valid"] is True


def test_session_route_rejects_bad_query_and_path(tmp_path):
    facade = _facade(tmp_path)

    bad_query = dispatch_session_route(facade, "/v1/sessions/s1/events", query={"page_size": ["nope"]})
    bad_path = dispatch_session_route(facade, "/v1/sessions/s1/unknown")

    assert bad_query.status_code == 400
    assert bad_query.body == {"error": "invalid_session_query"}
    assert bad_path.status_code == 404
    assert bad_path.body == {"error": "not_found"}


def test_http_composition_injects_typed_facade(monkeypatch, tmp_path):
    facade = _facade(tmp_path)
    assert serve.configure_session_facade(facade) is facade
    assert serve._SESSION_FACADE is facade
    source = open(serve.__file__, encoding="utf-8").read()
    assert "from server import" not in source
