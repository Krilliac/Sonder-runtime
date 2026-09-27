"""PreSendFallbackGateway: Sonder Inference -> Ollama, fail-closed.

The primary is a real SonderInferenceGateway over injected transports, so the
"provably not executed" decision is the production classifier's, not a stub's.
"""
from __future__ import annotations

import io
import json
import logging
import urllib.error

import pytest

from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
    SonderInferenceUnreachable,
)
from sonder_runtime.adapters.model_gateway_factory import build_model_gateway
from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env
from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.adapters.runtime_configuration import RuntimeConfig
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    InvalidInput,
)

HEALTH = {"status": "ready", "api_version": 1, "version": "0.1.0", "synthetic": False,
          "models": [{"id": "m", "backend": "ollama", "default": True}]}
CHAT = {"model": "m", "choices": [{"message": {"role": "assistant", "content": "from inference"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class FakeOllama:
    def __init__(self):
        self.calls: list[ModelRequest] = []

    def generate(self, request, context):
        self.calls.append(request)
        return ModelResponse(text="from ollama", model="sonder:latest", tier=request.tier,
                             duration_ms=1, tokens_in=1, tokens_out=1)

    def embed(self, texts, context):
        return []


def _ctx(**kw):
    return local_owner_context(correlation_id="turn-1", source="http", timeout_seconds=30, **kw)


def _primary(*, chat_error=None, health=HEALTH, health_status=200, get_error=None):
    posts = []

    def post(url, payload, headers, timeout):
        posts.append(payload)
        if chat_error is not None:
            raise chat_error
        return CHAT

    def get(url, headers, timeout):
        if get_error is not None:
            raise get_error
        return health_status, json.dumps(health).encode()

    gateway = SonderInferenceGateway(
        SonderInferenceConfig(base_url="http://127.0.0.1:18437"),
        transport=post, get_transport=get,
    )
    return gateway, posts


def _error(url, status, code):
    body = json.dumps({"error": {"message": "x", "type": "t", "code": code}}).encode()
    return urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(body))


REFUSED = urllib.error.URLError(ConnectionRefusedError(111, "refused"))


def test_healthy_primary_serves_and_fallback_is_untouched():
    primary, posts = _primary()
    ollama = FakeOllama()
    gateway = PreSendFallbackGateway(primary, fallback=ollama)
    assert gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx()).text == "from inference"
    assert ollama.calls == [] and len(posts) == 1 and gateway.fallback_count == 0


@pytest.mark.parametrize("scenario", ["health_refused", "send_refused", "not_ready", "starting"])
def test_provably_unexecuted_requests_fall_back_exactly_once(scenario, caplog):
    if scenario == "health_refused":
        primary, posts = _primary(get_error=REFUSED)
    elif scenario == "send_refused":
        primary, posts = _primary(chat_error=REFUSED)
    elif scenario == "not_ready":
        primary, posts = _primary(chat_error=_error("u", 503, "not_ready"))
    else:
        primary, posts = _primary(health={**HEALTH, "status": "starting"}, health_status=503)
    ollama = FakeOllama()
    observed = []
    gateway = PreSendFallbackGateway(
        primary, fallback=ollama, observer=lambda *args: observed.append(args[:3]),
    )
    request = ModelRequest(prompt="x", tier="fast")
    with caplog.at_level(logging.WARNING):
        response = gateway.generate(request, _ctx())
    assert response.text == "from ollama"
    assert ollama.calls == [request]  # the same request, once
    assert gateway.fallback_count == 1
    assert observed == [("sonder_inference", "ollama", "primary_unreachable")]
    assert any("provider fallback sonder_inference -> ollama" in r.message
               for r in caplog.records if r.levelno == logging.WARNING)
    assert len(posts) <= 1


@pytest.mark.parametrize("error,expected", [
    (TimeoutError("slow"), DeadlineExceeded),
    (_error("u", 400, "invalid_messages"), InvalidInput),
    (_error("u", 404, "model_not_found"), InvalidInput),
    (_error("u", 429, "overloaded"), CapacityExceeded),
    (_error("u", 500, "internal_error"), DependencyUnavailable),
    (_error("u", 503, "backend_unavailable"), DependencyUnavailable),
    (ConnectionResetError(104, "reset"), DependencyUnavailable),
])
def test_post_send_failures_never_reach_ollama(error, expected):
    primary, posts = _primary(chat_error=error)
    ollama = FakeOllama()
    gateway = PreSendFallbackGateway(primary, fallback=ollama)
    with pytest.raises(expected) as info:
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert not isinstance(info.value, SonderInferenceUnreachable)
    assert ollama.calls == [] and gateway.fallback_count == 0 and len(posts) == 1


def test_cancelled_operation_is_not_started_on_the_fallback():
    class Flip:
        def __init__(self):
            self.cancelled = False

        def wait(self, timeout=None):
            return self.cancelled

    token = Flip()

    class CancelOnRefusal:
        capabilities = frozenset()

        def generate(self, request, context):
            token.cancelled = True
            raise SonderInferenceUnreachable("refused")

    ollama = FakeOllama()
    gateway = PreSendFallbackGateway(CancelOnRefusal(), fallback=ollama)
    with pytest.raises(Cancelled):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx(cancellation=token))
    assert ollama.calls == [] and gateway.fallback_count == 0


def test_observer_failure_never_fails_the_turn():
    primary, _ = _primary(chat_error=REFUSED)

    def broken(*args):
        raise RuntimeError("telemetry down")

    gateway = PreSendFallbackGateway(primary, fallback=FakeOllama(), observer=broken)
    assert gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx()).text == "from ollama"


def test_status_reports_fallback_and_count_and_forwards_metadata():
    primary, _ = _primary(chat_error=REFUSED)
    gateway = PreSendFallbackGateway(primary, fallback=FakeOllama())
    gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    entry = gateway.provider_status()["sonder_inference"]
    assert (entry["fallback"], entry["fallback_count"]) == ("ollama", 1)
    assert gateway.capabilities == primary.capabilities
    assert gateway.request_admission is primary.request_admission
    assert gateway.resolve_route(ModelRequest(prompt="x", tier="fast"), _ctx()) is None
    assert gateway.capability_health().provider == "sonder_inference"
    with pytest.raises(DependencyUnavailable, match="SONDER_EMBEDDING_PROVIDER"):
        gateway.embed(["x"], _ctx())


def test_default_is_no_fallback_and_the_error_names_the_fix():
    bindings = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "sonder-inference", "SONDER_EMBEDDING_PROVIDER": "ollama",
    })
    primary, _ = _primary(get_error=REFUSED)
    ollama = FakeOllama()
    gateway = build_model_gateway(
        bindings, {"sonder_inference": lambda: primary, "ollama": lambda: ollama},
    )
    assert isinstance(gateway, ProviderDispatchGateway)
    with pytest.raises(SonderInferenceUnreachable) as info:
        gateway.generate(ModelRequest(prompt="x", tier="general"), _ctx())
    for needle in ("http://127.0.0.1:18437", "sonder-infer serve", "SONDER_INFERENCE_FALLBACK"):
        assert needle in str(info.value)
    assert ollama.calls == []


def test_factory_wraps_the_primary_and_shares_one_ollama_instance():
    bindings = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "sonder-inference",
        "SONDER_EMBEDDING_PROVIDER": "ollama",
        "SONDER_INFERENCE_FALLBACK": "ollama",
    })
    primary, _ = _primary(get_error=REFUSED)
    built = []

    def ollama_factory():
        built.append(FakeOllama())
        return built[-1]

    gateway = build_model_gateway(
        bindings, {"sonder_inference": lambda: primary, "ollama": ollama_factory},
    )
    assert len(built) == 1
    response = gateway.generate(ModelRequest(prompt="x", tier="code"), _ctx())
    assert response.text == "from ollama" and len(built[0].calls) == 1
    status = gateway.provider_status()
    assert status["sonder_inference"]["fallback_count"] == 1
    assert status["ollama"] == {"provider": "ollama", "state": "unknown"}


def test_fallback_target_is_not_a_routable_binding():
    bindings = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "sonder-inference",
        "SONDER_INFERENCE_FALLBACK": "ollama",
    })
    primary, _ = _primary()
    ollama = FakeOllama()
    gateway = build_model_gateway(
        bindings, {"sonder_inference": lambda: primary, "ollama": lambda: ollama},
    )
    # Uniform binding: the wrapper is returned directly; Ollama is reachable
    # only through the fallback, never via strict alias or tier dispatch.
    assert isinstance(gateway, PreSendFallbackGateway)
    assert gateway.generate(ModelRequest(prompt="x", tier="sonder"), _ctx()).text == "from inference"
    assert ollama.calls == []


def test_identity_bound_runtime_rejects_a_fallback_configuration(tmp_path):
    from sonder_runtime.adapters.runtime_capabilities import RuntimeCapabilities
    from sonder_runtime.adapters.runtime_container import build_runtime
    from sonder_runtime.application.model_gateway.health_and_roles import (
        LogicalRole,
        RoleBinding,
    )
    from sonder_runtime.application.routing.backend_conformance import (
        RecentCapabilityEvidence,
    )

    config = RuntimeConfig(
        model_backend="sonder-inference",
        provider_bindings=provider_bindings_from_env({
            "SONDER_MODEL_BACKEND": "sonder-inference",
            "SONDER_INFERENCE_FALLBACK": "ollama",
        }),
    )
    role = next(iter(LogicalRole))
    with pytest.raises(ValueError, match="fallback"):
        build_runtime(
            config, RuntimeCapabilities(),
            route_evidence=RecentCapabilityEvidence(tmp_path / "evidence.json"),
            route_identity_for=lambda route: None,
            route_bindings={role: RoleBinding(role, "sonder_inference", "m")},
        )


# -- the fallback never widens where a prompt goes -------------------------------


def test_fallback_never_reaches_a_cloud_model_even_with_cloud_consent(monkeypatch):
    from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway
    from sonder_runtime.application.ports.model_target import ModelTarget
    from sonder_runtime.domain.common.errors import Forbidden

    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    sent = []

    def resolver(tier, _):
        # Operator policy maps this tier to a hosted Ollama "-cloud" model.
        return ModelTarget(model="gpt-oss:120b-cloud", cloud=True, tier_label="reasoning")

    def factory(*args, **kwargs):
        def generate(*a, **k):
            sent.append((args, kwargs))
            return {"message": {"role": "assistant", "content": "cloud"}, "done_reason": "stop"}
        return generate

    ollama = OllamaGateway(target_resolver=resolver, generate_factory=factory)
    primary, posts = _primary(get_error=REFUSED)
    gateway = PreSendFallbackGateway(primary, fallback=ollama)
    with pytest.raises(Forbidden) as info:
        gateway.generate(ModelRequest(prompt="PRIVATE", tier="reasoning"), _ctx(cloud_allowed=True))
    assert sent == [] and posts == []
    # The refusal also says why the fallback was attempted at all.
    assert "connection refused" in str(info.value)


def test_fallback_runs_under_a_local_only_context():
    primary, _ = _primary(get_error=REFUSED)
    seen = []

    class Recording(FakeOllama):
        def generate(self, request, context):
            seen.append(context)
            return super().generate(request, context)

    gateway = PreSendFallbackGateway(primary, fallback=Recording())
    context = _ctx(cloud_allowed=True, remote_ollama_allowed=True)
    gateway.generate(ModelRequest(prompt="x", tier="fast"), context)
    (narrowed,) = seen
    assert narrowed.cloud_allowed is False and narrowed.remote_ollama_allowed is False
    assert (narrowed.correlation_id, narrowed.deadline_monotonic, narrowed.cancellation) == (
        context.correlation_id, context.deadline_monotonic, context.cancellation,
    )


def test_failed_fallback_reports_both_causes_and_keeps_the_error_class():
    primary, _ = _primary(get_error=REFUSED)

    class DownOllama(FakeOllama):
        def generate(self, request, context):
            raise DependencyUnavailable("Ollama unreachable at http://127.0.0.1:11434")

    gateway = PreSendFallbackGateway(primary, fallback=DownOllama())
    with pytest.raises(DependencyUnavailable) as info:
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    message = str(info.value)
    assert "Ollama unreachable" in message
    assert "fallback to ollama after" in message and "connection refused" in message
    assert not isinstance(info.value, SonderInferenceUnreachable)


def test_fallback_warning_states_the_cause_once(caplog):
    primary, _ = _primary(get_error=REFUSED)
    gateway = PreSendFallbackGateway(primary, fallback=FakeOllama())
    with caplog.at_level(logging.WARNING):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    (warning,) = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
                  and "provider fallback" in r.getMessage()]
    assert warning.count("not reachable") == 1
    assert "set SONDER_INFERENCE_FALLBACK" not in warning


@pytest.mark.parametrize("fallback", ["", "ollama"])
def test_strict_local_alias_is_refused_with_or_without_the_fallback(fallback):
    # Mirrors bootstrap/app.py: the strict-alias gate reads required_providers.
    from sonder_runtime.application.chat.handle_chat import ChatCommand, ChatService

    bindings = provider_bindings_from_env({
        "SONDER_MODEL_BACKEND": "sonder-inference", "SONDER_INFERENCE_FALLBACK": fallback,
    })
    primary, posts = _primary()
    ollama = FakeOllama()
    gateway = build_model_gateway(
        bindings, {"sonder_inference": lambda: primary, "ollama": lambda: ollama},
    )
    service = ChatService(
        gateway, chat_default_tier=lambda: "sonder",
        strict_alias_provider_available="ollama" in bindings.required_providers,
    )
    with pytest.raises(InvalidInput, match="strict chat alias"):
        service.complete(ChatCommand(content="hi", tier="sonder"), _ctx())
    assert posts == [] and ollama.calls == []
