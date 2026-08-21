"""MODEL-001 typed provider extraction tests."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.model_bootstrap import LegacyModelBootstrapAdapter
from sonder_runtime.application.ports.model_target import ModelTarget
from sonder_runtime.bootstrap.legacy_model import (
    configure_legacy_model_providers,
    lazy_legacy_model_provider_factories,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def test_adapter_preserves_legacy_target_and_generator_contract():
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def generate(*args, **kwargs):
        calls.append(("generate", args, kwargs))
        return lambda prompt, history=None: prompt

    runtime = SimpleNamespace(
        _serve_target=lambda tier, strict: ("model-x", True, False, tier),
        _make_generate=generate,
    )

    provider = LegacyModelBootstrapAdapter(runtime)
    assert provider.resolve_target("cloud-code", True) == ModelTarget(
        "model-x", True, "cloud-code", False
    )
    built = provider.make_generate(
        "model-x", "system", 0.2, 32, 4096,
        cloud=True, timeout=7, cancel_check=lambda: False, schema={"type": "object"},
    )

    assert built("hello") == "hello"
    assert calls == [
        (
            "generate",
            ("model-x", "system", 0.2, 32, 4096),
            {"cloud": True, "timeout": 7, "cancel_check": calls[0][2]["cancel_check"], "schema": {"type": "object"}},
        )
    ]


def test_lazy_factories_accept_an_injected_provider_without_loading_legacy_root(monkeypatch):
    calls: list[tuple[object, ...]] = []

    class Provider:
        def resolve_target(self, tier, strict=False):
            calls.append(("resolve", tier, strict))
            return ModelTarget("injected", False, tier, True)

        def make_generate(self, *args, **kwargs):
            calls.append(("generate", args, kwargs))
            return lambda prompt, history=None: "ok"

    def fail_import(*_args, **_kwargs):
        raise AssertionError("legacy root must not load for an injected provider")

    monkeypatch.setattr(
        "sonder_runtime.bootstrap.legacy_model._legacy_runtime", fail_import
    )
    resolve, generate = lazy_legacy_model_provider_factories(provider=Provider())
    assert resolve("code", True).model == "injected"
    assert generate("m", "s", 0.1, 2, 3)("prompt") == "ok"
    assert calls[0] == ("resolve", "code", True)


def test_configure_uses_provider_methods_as_gateway_injections(monkeypatch):
    captured = {}

    class Provider:
        def resolve_target(self, tier, strict=False):
            return ModelTarget("m", False, tier, True)

        def make_generate(self, *args, **kwargs):
            return lambda prompt, history=None: "ok"

    monkeypatch.setattr(
        "sonder_runtime.bootstrap.legacy_model.OllamaGateway.configure_default_providers",
        lambda **kwargs: captured.update(kwargs),
    )
    provider = Provider()
    configure_legacy_model_providers(provider=provider)
    assert captured == {
        "target_resolver": provider.resolve_target,
        "generate_factory": provider.make_generate,
    }


def test_new_typed_model_bootstrap_modules_have_no_root_imports():
    for relative in (
        "sonder_runtime/application/ports/model_bootstrap.py",
        "sonder_runtime/adapters/model_bootstrap.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "server" not in imports
        assert "sonder_runtime.bootstrap.legacy_root" not in imports
        assert "sonder_runtime.bootstrap.legacy_model" not in imports
