"""One served chat turn is captured once, by the surface that fails closed on it.

``sonder_serve`` captures a model-backed HTTP turn through
``_capture_live_session_turn`` under its own correlation id and turns a
capture failure into a failed request. ``server.answer_with_history`` also
captures the turns it answers for every other caller. Both ran for one HTTP
turn, so the session store held two records for it under two request ids.
The served runner now tells the legacy path not to capture.
"""
from __future__ import annotations

import pytest

import server
import sonder_runtime.interfaces.http.serve as ts

pytestmark = pytest.mark.unit


def test_the_served_runner_leaves_capture_to_the_served_surface(monkeypatch):
    seen = {}
    monkeypatch.setattr(ts.server, "parse_interaction_id", lambda out: None)
    monkeypatch.setattr(ts, "_strip_footer", lambda out: out)

    def fake_answer(prompt, history, **kwargs):
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)

    assert ts._run_prompt("hi", session="chat-1", project="app") == "ok"
    assert seen["capture_session"] is False


def test_the_legacy_answer_path_captures_unless_told_not_to(monkeypatch):
    captured = {}

    def fake_impl(*args, **kwargs):
        captured.update(kwargs)
        return "answer"

    monkeypatch.setattr(server, "_answer_with_history_impl", fake_impl)

    server.answer_with_history("hello", [], session="s1")
    assert captured["capture_session"] is True
    server.answer_with_history("hello", [], session="s1", capture_session=False)
    assert captured["capture_session"] is False


def test_the_capture_guard_sits_on_the_impl_capture_call():
    """The one legacy capture the served route disables is guarded by the flag."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(server.__file__).read_text(encoding="utf-8"))
    impl = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "_answer_with_history_impl")
    guarded = [
        node for node in ast.walk(impl)
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
        and node.test.id == "capture_session"
        and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "_capture_durable_session_turn"
                for call in ast.walk(node))
    ]
    assert len(guarded) == 1
    unguarded = [
        call for call in ast.walk(impl)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        and call.func.id == "_capture_durable_session_turn"
    ]
    assert len(unguarded) == 1, "a second capture call would reintroduce the double record"
