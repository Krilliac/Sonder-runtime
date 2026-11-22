"""Architecture and compatibility ratchets for the context-health slice."""
from __future__ import annotations

import ast
from pathlib import Path

from sonder_runtime.application.context_health import ContextHealthService


ROOT = Path(__file__).parents[1]


def test_context_health_application_is_root_free_and_port_driven():
    path = ROOT / "sonder_runtime" / "application" / "context_health.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    assert not any(
        isinstance(node, ast.Import) and any(a.name == "server" for a in node.names)
        or isinstance(node, ast.ImportFrom) and node.module == "server"
        for node in ast.walk(tree)
    )
    assert ContextHealthService.__module__ == "sonder_runtime.application.context_health"


def test_server_keeps_the_legacy_route_and_delegates_snapshot_computation():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    start = source.index("def context_health_data(")
    end = source.index("\ndef context_health(", start)
    slice_source = source[start:end]
    assert "ContextHealthService(" in slice_source
    assert "_resolve_session" in slice_source
    assert "_open_db()" in slice_source
    assert "ContextMemorySnapshot" in slice_source


def test_context_health_data_scopes_to_explicit_session_and_falls_back_to_session_events(
    monkeypatch, tmp_path
):
    import types
    import server
    from sonder_runtime.bootstrap.chat_tools_wiring import sonder_status_payload

    memory_store = server.memory_store

    db_path = tmp_path / "memory.db"
    resolved_project = server._resolve_project("")
    conn = memory_store.connect(db_path, check_same_thread=True)
    try:
        memory_store.log_interaction(
            conn,
            "int-default-1",
            "old default turn " * 120,
            "",
            "old default reply " * 120,
            "general",
            session_id="default",
            project=resolved_project,
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        server, "_open_db", lambda: memory_store.connect(db_path, check_same_thread=True)
    )
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    class _FakeRepo:
        def __init__(self):
            self._events = {
                "gui-thread-live": [
                    types.SimpleNamespace(
                        session_id="gui-thread-live",
                        sequence=1,
                        event_type="user.message",
                        payload={"content": "hello world " * 40},
                    ),
                    types.SimpleNamespace(
                        session_id="gui-thread-live",
                        sequence=2,
                        event_type="model.response",
                        payload={"content": "assistant reply " * 60},
                    ),
                ],
            }

        def read_tail(self, sid, limit=128):
            return list(self._events.get(sid, []))[-limit:]

    _repo_instance = _FakeRepo()

    class _FakeApp:
        @staticmethod
        def session_repository():
            return _repo_instance

        provider_health_data = staticmethod(lambda: ())

    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: _FakeApp())

    default_ctx = server.context_health_data(session="default")
    fresh_ctx = server.context_health_data(session="brand-new-empty-session")
    synced_ctx = server.context_health_data(session="gui-thread-live")

    assert default_ctx["estimated_tokens"] > 100
    assert fresh_ctx["session"] == "brand-new-empty-session"
    assert fresh_ctx["estimated_tokens"] == 0
    assert fresh_ctx["context_percent"] == 0.0
    assert synced_ctx["session"] == "gui-thread-live"
    assert synced_ctx["total_turns"] == 1
    assert synced_ctx["estimated_tokens"] > 100
    assert synced_ctx["context_percent"] > 0.0

    captured = {}

    class _AnyStub:
        def __call__(self, *a, **k):
            return {}

        def __getattr__(self, _name):
            return _AnyStub()

    class _StubServer:
        SESSION_NUM_CTX = 32768
        context_policy = server.context_policy

        def available_tiers(self):
            return {}

        def context_health_data(self, session="", project=""):
            captured["session"] = session
            captured["project"] = project
            return fresh_ctx

        def __getattr__(self, _name):
            return _AnyStub()

    sonder_status_payload(
        server=_StubServer(),
        context={"authorized": True, "account": {}},
        caps={"allowed_features": {}},
        public_hosting=False,
        admin_authorized=True,
        detail_allowed=True,
        memory_receiver_configured=False,
        app_control_binding={},
        state_home=str(tmp_path),
        deployment_payload={},
        build_operational_capabilities=lambda **k: {},
        narrate=lambda payload: payload,
        session="brand-new-empty-session",
        project="demo-proj",
    )
    assert captured == {"session": "brand-new-empty-session", "project": "demo-proj"}
