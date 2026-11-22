"""An ``openai_compatible`` rung is hosted unless its endpoint is verified loopback.

The provider name says nothing about where prompts go: the same binding serves
a llama.cpp server on this machine or a third-party API.  A remote endpoint
must get the hosted-data boundary (no local profile, goal, emotion vectors or
recalled memory), and anything that cannot be verified fails closed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import legacy_chat_bridge
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission
from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.application.chat import provider_bridge

_LOCAL = "http://127.0.0.1:18999"
_REMOTE = "https://api.example.com/v1"


def _openai(base_url):
    return OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url=base_url, model="fake"),
        transport=lambda *a, **k: pytest.fail("no request may be sent"),
        request_admission=HostModelRequestAdmission(None),
    )


def _dispatch(openai):
    tiers = {tier: "openai_compatible" for tier in ("fast", "general", "code")}
    return ProviderDispatchGateway(
        providers={"openai_compatible": openai, "ollama": SimpleNamespace()},
        tier_providers=tiers,
        default_generation_provider="openai_compatible",
        embedding_provider="ollama",
    )


@pytest.fixture
def probe_slot():
    """Restore whatever probe the composition root installed."""
    previous = provider_bridge.set_endpoint_locality_probe(None)
    try:
        yield
    finally:
        provider_bridge.set_endpoint_locality_probe(previous)


def test_endpoint_configured_provider_is_hosted_without_a_verified_loopback(probe_slot):
    assert provider_bridge.is_hosted("openai_compatible") is True  # no probe: fail closed
    provider_bridge.set_endpoint_locality_probe(lambda provider: True)
    assert provider_bridge.is_hosted("openai_compatible") is False
    provider_bridge.set_endpoint_locality_probe(lambda provider: False)
    assert provider_bridge.is_hosted("openai_compatible") is True
    provider_bridge.set_endpoint_locality_probe(lambda provider: "yes")
    assert provider_bridge.is_hosted("openai_compatible") is True  # only True verifies

    def broken(provider):
        raise RuntimeError("probe failed")

    provider_bridge.set_endpoint_locality_probe(broken)
    assert provider_bridge.is_hosted("openai_compatible") is True


def test_name_based_providers_ignore_the_probe(probe_slot):
    provider_bridge.set_endpoint_locality_probe(lambda provider: True)
    assert provider_bridge.is_hosted("openrouter") is True
    assert provider_bridge.is_hosted("anthropic") is True
    provider_bridge.set_endpoint_locality_probe(lambda provider: False)
    for provider in ("sonder_inference", "ollama", None, "", 3):
        assert provider_bridge.is_hosted(provider) is False


def test_hosted_rung_flag_follows_the_openai_compatible_endpoint(probe_slot):
    endpoint = {"loopback": True}
    provider_bridge.set_endpoint_locality_probe(lambda provider: endpoint["loopback"])
    with provider_bridge.bind_rung("openai_compatible", "general"):
        assert provider_bridge.hosted_rung_active() is False
        endpoint["loopback"] = False
        assert provider_bridge.hosted_rung_active() is True


@pytest.mark.parametrize("base_url,loopback", [
    (_LOCAL, True),
    ("http://localhost:8080/v1", True),
    ("http://[::1]:8080", True),
    (_REMOTE, False),
    ("http://10.0.0.5:8080", False),
    ("", False),
    ("not a url", False),
])
def test_gateway_reports_the_locality_of_the_endpoint_a_call_would_use(base_url, loopback):
    assert _openai(base_url).endpoint_is_loopback() is loopback


def test_env_resolved_gateway_reads_the_endpoint_at_call_time(monkeypatch):
    gateway = OpenAICompatibleGateway(request_admission=HostModelRequestAdmission(None))
    monkeypatch.delenv("SONDER_OPENAI_BASE_URL", raising=False)
    assert gateway.endpoint_is_loopback() is False
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", _LOCAL)
    assert gateway.endpoint_is_loopback() is True
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", _REMOTE)
    assert gateway.endpoint_is_loopback() is False


def test_graph_probe_asks_the_gateway_that_serves_the_provider(monkeypatch):
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", _LOCAL)  # must not override the graph
    remote = SimpleNamespace(model_gateway=_dispatch(_openai(_REMOTE)))
    local = SimpleNamespace(model_gateway=_dispatch(_openai(_LOCAL)))
    direct = SimpleNamespace(model_gateway=_openai(_LOCAL))
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", remote) is False
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", local) is True
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", direct) is True
    fallback = SimpleNamespace(model_gateway=PreSendFallbackGateway(
        _openai(_REMOTE), fallback=SimpleNamespace(),
        primary_id="openai_compatible", fallback_id="ollama",
    ))
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", fallback) is False


def test_graph_probe_fails_closed_on_a_gateway_it_cannot_identify(monkeypatch):
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", _LOCAL)
    unknown = SimpleNamespace(model_gateway=SimpleNamespace(generate=lambda *a: None))
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", unknown) is False
    unbound = SimpleNamespace(model_gateway=ProviderDispatchGateway(
        providers={"ollama": SimpleNamespace()}, tier_providers={"fast": "ollama"},
        default_generation_provider="ollama", embedding_provider="ollama",
    ))
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", unbound) is False


def test_graph_probe_before_a_graph_exists_reads_the_env_endpoint(monkeypatch):
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", _LOCAL)
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", None) is True
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", _REMOTE)
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", None) is False
    monkeypatch.delenv("SONDER_OPENAI_BASE_URL")
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", None) is False


def test_server_installs_the_graph_probe(monkeypatch):
    import server

    monkeypatch.setattr(server, "_APP_GRAPH",
                        SimpleNamespace(model_gateway=_dispatch(_openai(_REMOTE))))
    assert provider_bridge.is_hosted("openai_compatible") is True
    monkeypatch.setattr(server, "_APP_GRAPH",
                        SimpleNamespace(model_gateway=_dispatch(_openai(_LOCAL))))
    assert provider_bridge.is_hosted("openai_compatible") is False


def test_remote_openai_compatible_rung_gets_no_local_memory(monkeypatch):
    """Recall, lessons and facts never reach a third-party OpenAI-compatible API."""
    import server

    class Stop(Exception):
        pass

    touched = []

    def local_memory(*args, **kwargs):
        touched.append("memory")
        raise Stop()

    class App:
        class recall:
            retrieve = staticmethod(local_memory)

    def generate(*args, **kwargs):
        def run(*call_args, **call_kwargs):
            touched.append("generate")
            raise Stop()
        return run

    monkeypatch.setattr(server.embeddings, "embed", lambda prompt: None)
    monkeypatch.setattr(server, "_application", lambda: App)
    monkeypatch.setattr(server, "_preference_facts", local_memory)
    monkeypatch.setattr(server, "_make_generate", generate)

    def answer():
        with pytest.raises(Stop):
            server._answer(None, "hello", "m/x", "", 0.2, 64, 0, None, "proj", None,
                           tier="general", augment=True)

    monkeypatch.setattr(server, "_APP_GRAPH",
                        SimpleNamespace(model_gateway=_dispatch(_openai(_LOCAL))))
    with provider_bridge.bind_rung("openai_compatible", "general"):
        answer()
    assert touched == ["memory"]  # non-vacuity: a loopback endpoint does recall
    touched.clear()
    monkeypatch.setattr(server, "_APP_GRAPH",
                        SimpleNamespace(model_gateway=_dispatch(_openai(_REMOTE))))
    with provider_bridge.bind_rung("openai_compatible", "general"):
        answer()
    assert touched == ["generate"]


def test_remote_openai_compatible_rung_system_prompt_has_no_local_profile(monkeypatch):
    import server

    monkeypatch.setattr(server, "_read_system_context",
                        lambda: ("LOCAL-PROFILE", "LOCAL-EMOTIONS", "LOCAL-GOAL"))
    monkeypatch.setattr(server, "_APP_GRAPH",
                        SimpleNamespace(model_gateway=_dispatch(_openai(_LOCAL))))
    local = server._build_system("base rules", False, "", model="qwen", cloud=False,
                                 provider="openai_compatible")
    assert "LOCAL-PROFILE" in local  # non-vacuity: a loopback endpoint keeps it
    monkeypatch.setattr(server, "_APP_GRAPH",
                        SimpleNamespace(model_gateway=_dispatch(_openai(_REMOTE))))
    hosted = server._build_system("base rules", False, "", model="qwen", cloud=False,
                                  provider="openai_compatible")
    for marker in ("LOCAL-PROFILE", "LOCAL-EMOTIONS", "LOCAL-GOAL"):
        assert marker not in hosted
    assert "base rules" in hosted


@pytest.mark.parametrize("url", [
    "http://0.0.0.0:8080",
    "ftp://127.0.0.1:8080",
    "http://user:pass@127.0.0.1:8080",
])
def test_unusable_or_unspecified_endpoints_are_not_verified_loopback(url):
    assert _openai(url).endpoint_is_loopback() is False


def test_subclass_gateways_do_not_inherit_the_openai_env_probe(monkeypatch):
    from sonder_runtime.adapters.inference.openrouter_gateway import OpenRouterGateway
    from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceGateway

    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", _LOCAL)
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "https://remote-inference.example.com")
    assert OpenRouterGateway().endpoint_is_loopback() is False
    assert SonderInferenceGateway().endpoint_is_loopback() is False


_HOSTILE_URLS = [
    "http://0.0.0.0:8080",
    "ftp://127.0.0.1:8080",
    "file://localhost/x",
    "http://user:pass@127.0.0.1:8080",
    "http://@127.0.0.1",
    "//127.0.0.1:8080",
]


@pytest.mark.parametrize("url", _HOSTILE_URLS)
def test_graph_none_probe_rejects_unusable_loopback_urls(monkeypatch, url):
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", url)
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", None) is False
    assert provider_bridge.is_hosted("openai_compatible") is True


@pytest.mark.parametrize("url", _HOSTILE_URLS)
def test_consent_gate_agrees_with_the_hostedness_probe(url):
    from sonder_runtime.adapters.inference.openai_compat_gateway import OpenAICompatibleGateway

    assert OpenAICompatibleGateway._is_loopback(url) is False


def test_probe_is_false_when_graph_does_not_bind_the_provider():
    from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceGateway
    from sonder_runtime.adapters.provider_bindings import ProviderBindings

    inf = SonderInferenceGateway(
        env={"SONDER_INFERENCE_BASE_URL": "http://127.0.0.1:9"},
        request_admission=HostModelRequestAdmission(None),
    )
    graph = SimpleNamespace(
        model_gateway=inf, provider_bindings=ProviderBindings.uniform("sonder_inference"))
    assert legacy_chat_bridge.endpoint_is_loopback("openai_compatible", graph) is False
