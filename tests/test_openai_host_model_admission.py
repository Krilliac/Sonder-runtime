"""OpenAI-compatible physical sends share one startup host rate authority."""

import urllib.error

import pytest

import server
from sonder_runtime.adapters import model_request_admission
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.model_gateway_factory import build_model_gateway
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import Cancelled, CapacityExceeded, Forbidden


def _policy(tmp_path, burst=2):
    return model_request_admission.HostModelRequestAdmission.from_environ(
        {"SONDER_MODEL_REQUEST_BURST": str(burst),
         "SONDER_MODEL_REQUESTS_PER_MINUTE": "1"},
        clock=lambda: 100.0,
        db_path=tmp_path / "rate.db",
    )


def _cfg(remote=False):
    host = "provider.example" if remote else "127.0.0.1"
    return OpenAICompatibleConfig(base_url=f"http://{host}:8080", model="chat")


def _response():
    return {"choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4}}


def test_ollama_then_openai_share_the_same_host_physical_request_bucket(monkeypatch, tmp_path):
    policy = _policy(tmp_path)
    monkeypatch.setattr(server, "_HOST_MODEL_REQUEST_ADMISSION", policy)
    monkeypatch.setattr(server, "_require_ollama_endpoint", lambda **_: None)
    seen = []
    monkeypatch.setattr(server, "_post", lambda *_args, **_kw: seen.append("ollama") or {
        "message": {"content": "ok"}
    })
    gateway = OpenAICompatibleGateway(
        _cfg(), transport=lambda *_: seen.append("openai") or _response(),
        request_admission=policy,
    )
    context = local_owner_context(correlation_id="cross-provider")
    server._post_model("/api/chat", {}, model="sonder:latest")
    assert gateway.generate(ModelRequest("q", tier="code"), context).tokens_out == 4
    with pytest.raises(CapacityExceeded, match="host model request rate"):
        gateway.generate(ModelRequest("q", tier="code"), context)
    with pytest.raises(ModelCallError, match="host model request rate"):
        server._post_model("/api/chat", {}, model="sonder:latest")
    assert seen == ["ollama", "openai"]


def test_openai_refusal_and_cancellation_do_not_send_or_report_fake_usage(tmp_path):
    policy = _policy(tmp_path, burst=1)
    seen = []
    gateway = OpenAICompatibleGateway(
        _cfg(), transport=lambda *_: seen.append("send") or _response(),
        request_admission=policy,
    )

    class Cancel:
        cancelled = True

        def wait(self, _timeout=None):
            return True

    cancelled = local_owner_context(
        correlation_id="cancelled", cancellation=Cancel()
    )
    with pytest.raises(Cancelled):
        gateway.generate(ModelRequest("q", tier="code"), cancelled)
    with pytest.raises(Forbidden):
        OpenAICompatibleGateway(
            _cfg(remote=True), transport=lambda *_: seen.append("bad"),
            request_admission=policy,
        ).generate(ModelRequest("private", tier="code"),
                   local_owner_context(correlation_id="no-consent"))
    assert gateway.generate(
        ModelRequest("q", tier="code"),
        local_owner_context(correlation_id="success"),
    ).tokens_out == 4
    with pytest.raises(CapacityExceeded):
        gateway.generate(
            ModelRequest("q", tier="code"),
            local_owner_context(correlation_id="blocked"),
        )
    assert seen == ["send"]


def test_openai_failed_send_then_next_send_each_consume_a_token(tmp_path):
    policy = _policy(tmp_path, burst=2)
    seen = []

    def transport(*_args):
        seen.append("physical-send")
        if len(seen) == 1:
            raise urllib.error.URLError("transient failure")
        return _response()

    gateway = OpenAICompatibleGateway(
        _cfg(), transport=transport, request_admission=policy
    )
    context = local_owner_context(correlation_id="retry")
    from sonder_runtime.domain.common.errors import DependencyUnavailable

    with pytest.raises(DependencyUnavailable):
        gateway.generate(ModelRequest("q", tier="code"), context)
    # The OpenAI adapter does not automatically retry a metered send; its
    # caller's next invocation is a separate charged physical attempt.
    assert gateway.generate(ModelRequest("q", tier="code"), context).text == "ok"
    with pytest.raises(CapacityExceeded):
        gateway.generate(ModelRequest("q", tier="code"), context)
    assert seen == ["physical-send", "physical-send"]


def test_standalone_openai_host_factory_uses_the_same_process_authority(monkeypatch, tmp_path):
    policy = _policy(tmp_path, burst=1)
    monkeypatch.setattr(
        model_request_admission, "_HOST_MODEL_REQUEST_ADMISSION", policy
    )
    gateway = build_model_gateway(backend="openai")
    assert isinstance(gateway, OpenAICompatibleGateway)
    assert gateway.request_admission is policy


def test_embedding_and_chat_use_one_physical_request_bucket(tmp_path):
    policy = _policy(tmp_path, burst=1)
    sends = []

    def transport(url, *_args):
        sends.append(url)
        return {"data": [{"index": 0, "embedding": [0.25]}]}

    gateway = OpenAICompatibleGateway(
        _cfg(), transport=transport, request_admission=policy
    )
    context = local_owner_context(correlation_id="embed-then-chat")
    assert gateway.embed(["q"], context)[0].vector == (0.25,)
    with pytest.raises(CapacityExceeded, match="host model request rate"):
        gateway.generate(ModelRequest("q", tier="code"), context)
    assert len(sends) == 1 and sends[0].endswith("/v1/embeddings")


def test_cancellation_race_before_physical_send_keeps_token_available(tmp_path):
    policy = _policy(tmp_path, burst=1)
    sends = []

    class Cancel:
        cancelled = False

        def wait(self, _timeout=None):
            return False

    token = Cancel()
    gateway = OpenAICompatibleGateway(
        _cfg(), transport=lambda *_: sends.append(1) or _response(),
        request_admission=policy,
    )
    original = gateway._build_messages

    def cancel_during_build(request):
        token.cancelled = True
        return original(request)

    gateway._build_messages = cancel_during_build
    with pytest.raises(Cancelled):
        gateway.generate(
            ModelRequest("first", tier="code"),
            local_owner_context(correlation_id="cancel-during-build", cancellation=token),
        )
    gateway._build_messages = original
    assert gateway.generate(
        ModelRequest("second", tier="code"),
        local_owner_context(correlation_id="after-cancel"),
    ).text == "ok"
    assert sends == [1]
