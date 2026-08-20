"""Regression coverage for the canonical SPEC-5 runtime container adapter."""
from __future__ import annotations

from sonder_runtime.adapters.runtime_capabilities import RuntimeCapabilities
from sonder_runtime.adapters.runtime_configuration import RuntimeConfig
from sonder_runtime.adapters.runtime_container import Runtime, build_runtime
from sonder_runtime.bootstrap.container import (
    Runtime as CompatibilityRuntime,
    build_runtime as compatibility_build_runtime,
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
