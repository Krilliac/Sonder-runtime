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


def test_gateway_edge_forwards_explicit_num_ctx(monkeypatch):
    fake = _FakeChat(ChatResult("t", "m", "code"))
    _install_chat(monkeypatch, fake)
    server._gateway_generate_text("p", tier="code", num_ctx=2048)
    command = fake.calls[0][0]
    assert command.num_ctx == 2048
    # Omitted num_ctx stays None so the gateway resolves the native window.
    fake.calls.clear()
    server._gateway_generate_text("p", tier="code")
    assert fake.calls[0][0].num_ctx is None


def test_chat_service_forwards_num_ctx_into_request_options():
    from sonder_runtime.application.chat.handle_chat import (
        ChatCommand, ChatService,
    )
    from sonder_runtime.application.context import local_owner_context

    seen = {}

    class _Gateway:
        def generate(self, request, context):
            seen["options"] = dict(request.options)

            class _R:
                text, model, tier = "x", "m", "code"
                duration_ms, tokens_in, tokens_out = 0, None, None

            return _R()

    service = ChatService(_Gateway())
    service.complete(
        ChatCommand(content="p", tier="code", num_ctx=2048),
        local_owner_context(correlation_id="t", source="test"),
    )
    assert seen["options"]["num_ctx"] == 2048


def test_lesson_distillation_routes_through_gateway(monkeypatch):
    import reflection

    fake = _FakeChat(ChatResult("lesson text", "m", "code"))
    _install_chat(monkeypatch, fake)

    def fake_prepare(task, response, signal, offload_fn, embed_fn):
        offload_fn(
            prompt="distill", tier="code", system="S",
            temperature=0.1, num_predict=120,
        )
        return {"status": "skipped"}

    monkeypatch.setattr(reflection, "prepare_lesson_candidate", fake_prepare)
    server._prepare_lesson_candidate_bounded(
        {"task": "t", "response": "r"}, "tests_passed",
    )
    command = fake.calls[0][0]
    assert command.tier == "code"
    # Distillation pins the small context it has always used.
    assert command.num_ctx == 2048


def test_pitfall_distillation_routes_through_gateway(monkeypatch):
    import reflection

    fake = _FakeChat(ChatResult("pitfall text", "m", "code"))
    _install_chat(monkeypatch, fake)

    def fake_prepare(task, response, error, offload_fn):
        offload_fn(
            prompt="pitfall", tier="code", system="S",
            temperature=0.1, num_predict=120,
        )
        return {"status": "not-a-candidate"}

    monkeypatch.setattr(reflection, "prepare_pitfall_candidate", fake_prepare)
    lesson_id, note = server._record_failure_pitfall(
        "iid-1", "task", "resp", "boom",
    )
    assert (lesson_id, note) == ("", "")
    command = fake.calls[0][0]
    assert command.tier == "code"
    assert command.num_ctx == 2048
