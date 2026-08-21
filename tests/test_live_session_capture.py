from __future__ import annotations

import sys
import types

import pytest

from sonder_runtime.interfaces.http import serve


class _Capture:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def capture_turn(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("persistence unavailable")
        self.calls.append((args, kwargs))
        return "captured"


def _install_app(monkeypatch, capture):
    module = types.ModuleType("sonder_runtime.bootstrap.app")
    module.default_app = lambda: types.SimpleNamespace(
        session_capture_service=lambda: capture
    )
    monkeypatch.setitem(sys.modules, "sonder_runtime.bootstrap.app", module)


def test_live_named_model_turn_uses_typed_capture(monkeypatch):
    capture = _Capture()
    _install_app(monkeypatch, capture)

    result = serve._capture_live_session_turn(
        session_id="account-session",
        prompt="hello",
        history=({"role": "user", "content": "prior"},),
        model="local-model",
        content="world",
        request_id="request-1",
        turn_id="turn-1",
        stream=False,
    )

    assert result == "captured"
    args, kwargs = capture.calls[0]
    assert args[:2] == ("account-session", "turn-1")
    assert kwargs["request_id"] == "request-1"
    assert kwargs["user_message"] == "hello"
    assert kwargs["model_response"] == "world"
    assert args[2].prompt == "hello"
    assert args[2].history[0]["content"] == "prior"


def test_live_capture_failure_is_opaque_and_fail_closed(monkeypatch):
    _install_app(monkeypatch, _Capture(fail=True))

    with pytest.raises(serve._LiveSessionCaptureFailure):
        serve._capture_live_session_turn(
            session_id="account-session",
            prompt="hello",
            history=(),
            model="local-model",
            content="world",
            request_id="request-1",
            turn_id="turn-1",
            stream=True,
        )


def test_unnamed_model_turn_does_not_persist(monkeypatch):
    capture = _Capture()
    _install_app(monkeypatch, capture)

    assert serve._capture_live_session_turn(
        session_id="",
        prompt="hello",
        history=(),
        model="local-model",
        content="world",
        request_id="request-1",
        turn_id="turn-1",
        stream=False,
    ) is None
    assert capture.calls == []
