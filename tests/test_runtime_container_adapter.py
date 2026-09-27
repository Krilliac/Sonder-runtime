"""Regression coverage for the canonical SPEC-5 runtime container adapter."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.adapters.runtime_capabilities import RuntimeCapabilities
from sonder_runtime.adapters.runtime_configuration import RuntimeConfig
from sonder_runtime.adapters.runtime_container import Runtime, build_runtime
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.model_gateway.health_and_roles import (
    LogicalRole,
    ProviderHealth,
    ProviderState,
    RoleBinding,
)
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.routing.backend_conformance import (
    RecentCapabilityEvidence,
)
from sonder_runtime.bootstrap.container import (
    Runtime as CompatibilityRuntime,
)
from sonder_runtime.bootstrap.container import (
    build_runtime as compatibility_build_runtime,
)
from sonder_runtime.bootstrap.provider_bindings import ProviderBindings
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability,
    BackendConformanceRecord,
    BackendIdentity,
    ProbeResult,
)


def _config(backend: str = "ollama") -> RuntimeConfig:
    return RuntimeConfig(
        profile="workstation-local",
        model_backend=backend,
    )


def test_bootstrap_container_preserves_runtime_compatibility_identity():
    assert CompatibilityRuntime is Runtime
    assert compatibility_build_runtime is build_runtime


def test_runtime_container_selects_ollama_gateway_without_eager_network_io():
    runtime = build_runtime(_config(), RuntimeCapabilities())
    assert isinstance(runtime, Runtime)
    assert runtime.config.model_backend == "ollama"
    assert runtime.model_gateway.__class__.__name__ == "OllamaGateway"


def test_runtime_container_selects_openai_compatible_gateway():
    runtime = build_runtime(_config("openai-compatible"), RuntimeCapabilities())
    assert isinstance(runtime, Runtime)
    assert runtime.model_gateway.__class__.__name__ == "OpenAICompatibleGateway"


def test_runtime_container_composes_configured_mixed_provider_bindings():
    bindings = ProviderBindings(
        default_generation_provider="ollama",
        tier_providers={
            "fast": "openai_compatible",
            "general": "openai_compatible",
            "code": "ollama",
            "reasoning": "ollama",
            "vision": "ollama",
        },
        embedding_provider="ollama",
    )
    runtime = build_runtime(
        RuntimeConfig(profile="workstation-local", provider_bindings=bindings),
        RuntimeCapabilities(),
    )

    assert isinstance(runtime.model_gateway, ProviderDispatchGateway)
    assert runtime.provider_bindings is bindings


def test_runtime_container_derives_protocol_schema_from_the_tool_catalog():
    runtime = build_runtime(_config(), RuntimeCapabilities())

    assert runtime.protocol is not None
    assert runtime.protocol.schema.source_catalog_digest == runtime.tools.catalogs.digest
    assert runtime.protocol.schema.stream["kind"] == "snapshot-plus-events"


def test_opted_in_runtime_gateway_denies_synthetic_and_changed_host_identity_before_call(
    tmp_path, monkeypatch,
):
    import sonder_runtime.adapters.inference.model_gateway_factory as factory

    class Gateway:
        def generate(self, request, context):
            pytest.fail("provider was called without current live conformance evidence")

        def embed(self, texts, context):
            pytest.fail("embedding provider was called without current evidence")

    gateway = Gateway()
    monkeypatch.setattr(factory, "build_model_gateway", lambda bindings: gateway)
    current = {"identity": BackendIdentity(
        backend="ollama", model="fixture", model_digest="a" * 64,
        quantization="Q4", backend_version="v1", tokenizer_digest="b" * 64,
        template_digest="c" * 64, context_tokens=8192, hardware="cpu-1",
    )}
    evidence = RecentCapabilityEvidence(tmp_path / "capabilities.json")
    evidence.save(BackendConformanceRecord(
        "ollama", "fixture", 100, (ProbeResult(BackendCapability.CHAT, True, "observed"),),
        synthetic=True, identity=current["identity"],
    ))
    runtime = build_runtime(
        _config(), RuntimeCapabilities(), route_evidence=evidence,
        route_identity_for=lambda route: current["identity"],
        route_bindings={
            LogicalRole.DEFAULT: RoleBinding(LogicalRole.DEFAULT, "ollama", "fixture"),
        },
        route_health={
            "ollama": ProviderHealth(
                "ollama", ProviderState.READY, datetime.now(timezone.utc),
            ),
        },
    )
    assert runtime.model_gateway is runtime.model_routes
    request = ModelRequest("test", "fixture")
    context = local_owner_context(correlation_id="strict-runtime")
    with pytest.raises(DependencyUnavailable, match="synthetic_capability_evidence"):
        runtime.model_gateway.generate(request, context)
    current["identity"] = replace(current["identity"],
                                  template_digest="d" * 64, hardware="gpu-2")
    with pytest.raises(DependencyUnavailable, match="backend_identity_changed"):
        runtime.model_gateway.generate(request, context)
    with pytest.raises(DependencyUnavailable):
        runtime.model_gateway.embed(["test"], context)


def test_opted_in_runtime_refuses_unbound_or_mixed_provider_dispatch(tmp_path):
    evidence = RecentCapabilityEvidence(tmp_path / "evidence.json")
    identity_for = lambda route: None
    health = {"ollama": ProviderHealth(
        "ollama", ProviderState.READY, datetime.now(timezone.utc),
    )}
    bind = lambda name: {LogicalRole.DEFAULT: RoleBinding(
        LogicalRole.DEFAULT, name, "fixture",
    )}
    with pytest.raises(ValueError, match="configured provider"):
        build_runtime(
            _config(), RuntimeCapabilities(), route_evidence=evidence,
            route_identity_for=identity_for, route_bindings=bind("openai-compatible"),
            route_health=health,
        )
    mixed = ProviderBindings(
        default_generation_provider="ollama",
        tier_providers={tier: "openai_compatible" if tier == "fast" else "ollama"
                        for tier in ("fast", "general", "code", "reasoning", "vision")},
        embedding_provider="ollama",
    )
    with pytest.raises(ValueError, match="single concrete provider"):
        build_runtime(
            RuntimeConfig(profile="workstation-local", provider_bindings=mixed),
            RuntimeCapabilities(), route_evidence=evidence,
            route_identity_for=identity_for, route_bindings=bind("ollama"),
            route_health=health,
        )
    openai_label = "openai-compatible"
    compatible = build_runtime(
        _config(openai_label), RuntimeCapabilities(), route_evidence=evidence,
        route_identity_for=identity_for, route_bindings=bind(openai_label),
        route_health={openai_label: ProviderHealth(
            openai_label, ProviderState.READY, datetime.now(timezone.utc),
        )},
    )
    assert compatible.model_gateway is compatible.model_routes


@pytest.mark.parametrize("alias", ["sonder-inference", "sonder_inference", "inference"])
def test_runtime_container_accepts_sonder_inference_aliases(alias):
    runtime = build_runtime(_config(alias), RuntimeCapabilities())
    assert runtime.model_gateway.__class__.__name__ == "SonderInferenceGateway"
    assert runtime.provider_bindings.required_providers == frozenset({"sonder_inference"})


def test_runtime_configuration_captures_the_inference_fallback():
    from sonder_runtime.adapters.runtime_configuration import build_config_from_env

    config = build_config_from_env("local", {
        "SONDER_MODEL_BACKEND": "sonder-inference",
        "SONDER_INFERENCE_FALLBACK": "ollama",
    })
    assert config.provider_bindings is not None
    assert dict(config.provider_bindings.fallbacks) == {"sonder_inference": "ollama"}
    runtime = build_runtime(config, RuntimeCapabilities())
    assert runtime.model_gateway.__class__.__name__ == "PreSendFallbackGateway"
