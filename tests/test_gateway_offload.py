"""The session summarize/title offload path routed through the ModelGateway."""
from __future__ import annotations

import urllib.error

import pytest

import model_transport
import server
from sonder_runtime.application.chat.handle_chat import ChatResult
from sonder_runtime.domain.common.errors import (
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
)


@pytest.fixture(autouse=True)
def fresh_app_graph(monkeypatch):
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    yield
    monkeypatch.setattr(server, "_APP_GRAPH", None)


class _FakeChat:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = []

    def complete(self, command, context):
        self.calls.append((command, context))
        if self._error is not None:
            raise self._error
        return self._result


def _install_chat(monkeypatch, fake):
    class _App:
        chat = fake

    monkeypatch.setattr(server, "_application", lambda: _App())


def test_offload_returns_gateway_text(monkeypatch):
    fake = _FakeChat(ChatResult(
        response_text="a terse summary", model="qwen2.5:0.5b", tier="fast",
    ))
    _install_chat(monkeypatch, fake)
    out = server._gateway_generate_text("summarize this", tier="fast")
    assert out == "a terse summary"
    # The command and a real operation context reached the port.
    command, context = fake.calls[0]
    assert command.tier == "fast"
    assert context.correlation_id.startswith("offload-")


def test_consent_gate_is_carried_from_runtime(monkeypatch):
    fake = _FakeChat(ChatResult("x", "m", "fast"))
    _install_chat(monkeypatch, fake)
    monkeypatch.setattr(server, "cloud_allowed", lambda: False)
    server._gateway_generate_text("p", tier="fast")
    assert fake.calls[0][1].cloud_allowed is False
    fake.calls.clear()
    monkeypatch.setattr(server, "cloud_allowed", lambda: True)
    server._gateway_generate_text("p", tier="fast")
    assert fake.calls[0][1].cloud_allowed is True


@pytest.mark.parametrize("domain_error,expected_kind", [
    (DeadlineExceeded("slow"), "timeout"),
    (DependencyUnavailable("ollama down"), "request"),
    (Forbidden("cloud off"), "configuration"),
])
def test_domain_errors_translate_to_modelcallerror(
    monkeypatch, domain_error, expected_kind,
):
    _install_chat(monkeypatch, _FakeChat(error=domain_error))
    with pytest.raises(model_transport.ModelCallError) as excinfo:
        server._gateway_generate_text("p", tier="fast")
    assert excinfo.value.kind == expected_kind
    # ModelCallError is a URLError subclass, so summarizer's existing
    # `except urllib.error.URLError` still catches it.
    assert isinstance(excinfo.value, urllib.error.URLError)


def test_summarizer_survives_gateway_failure(monkeypatch):
    # End-to-end: session summarization catches the translated URLError and
    # keeps the prior summary (unchanged behavior).
    import summarizer

    def boom(**kwargs):
        raise DependencyUnavailable("down")

    _install_chat(monkeypatch, _FakeChat(error=DependencyUnavailable("down")))
    # summarize() itself calls offload_fn; the caller catches URLError.
    try:
        summarizer.summarize("prior", [("t", "r")], server._gateway_generate_text)
    except urllib.error.URLError:
        pass  # expected: translated domain error, caught exactly as before
    else:
        pytest.fail("expected a URLError-family error from the gateway edge")


def test_bootstrap_graph_exposes_chat_over_gateway():
    from sonder_runtime.bootstrap import app as bootstrap_app
    from sonder_runtime.adapters.ollama.gateway import OllamaGateway

    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application()
    assert application.chat is not None
    assert isinstance(application.model_gateway, OllamaGateway)
    bootstrap_app.reset_for_tests()
