"""WP1 model-adapter root-removal contract tests."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sonder_runtime.adapters.legacy_model_gateway import LegacyModelGateway
from sonder_runtime.adapters.ollama.gateway import OllamaGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.ports.model_target import ModelTarget
from sonder_runtime.domain.common.errors import DependencyUnavailable


ROOT = Path(__file__).parents[1]
_CONTEXT = local_owner_context(correlation_id="wp1-model-root-removal")


def _imports_root_server(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "server" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "server":
            return True
    return False


def test_model_adapters_have_no_direct_server_import():
    assert not _imports_root_server(
        ROOT / "sonder_runtime" / "adapters" / "legacy_model_gateway.py"
    )
    assert not _imports_root_server(
        ROOT / "sonder_runtime" / "adapters" / "ollama" / "gateway.py"
    )


def test_legacy_gateway_preserves_request_shape_with_injected_provider():
    calls = []

    def generate(prompt, *, history=None, tier=None):
        calls.append((prompt, history, tier))
        return "legacy response"

    gateway = LegacyModelGateway(generate=generate, embed=lambda value: [len(value)])
    response = gateway.generate(
        ModelRequest("hello", "code", history=(("user", "prior"),)), _CONTEXT
    )

    assert response.text == "legacy response"
    assert response.model == "code"
    assert calls == [("hello", [("user", "prior")], "code")]
    assert gateway.embed(["a"], _CONTEXT)[0].vector == (1.0,)


def test_legacy_gateway_fails_closed_without_injected_chat_provider():
    with pytest.raises(DependencyUnavailable, match="injected generate"):
        LegacyModelGateway().generate(ModelRequest("hello", "code"), _CONTEXT)


def test_ollama_gateway_preserves_target_system_and_generation_shape():
    calls = []

    def resolve(tier, strict=False):
        assert (tier, strict) == ("code", False)
        return ModelTarget("provider-model", False, "code")

    def build_system(system, trace, persona, *, model="", cloud=False):
        assert (system, trace, persona, model, cloud) == ("", False, "", "provider-model", False)
        return "provider system"

    def make_generate(model, system, temperature, num_predict, num_ctx, **kwargs):
        calls.append((model, system, temperature, num_predict, num_ctx, kwargs))

        def generate(prompt, history=None):
            assert (prompt, history) == ("hello", [("user", "prior")])
            generate.last_usage = {"tokens_in": 3, "tokens_out": 2}
            generate.last_response_meta = {}
            return "ollama response"

        generate.last_usage = {}
        generate.last_response_meta = {}
        return generate

    gateway = OllamaGateway(
        target_resolver=resolve,
        system_builder=build_system,
        generate_factory=make_generate,
        embedding_provider=lambda text: [0.25],
        session_num_ctx=4096,
    )
    response = gateway.generate(
        ModelRequest(
            "hello", "code", history=(("user", "prior"),), options={"num_predict": 12}
        ),
        _CONTEXT,
    )

    assert response.text == "ollama response"
    assert response.model == "provider-model"
    assert response.tokens_in == 3 and response.tokens_out == 2
    assert calls[0][0:5] == ("provider-model", "provider system", 0.2, 12, 4096)
    assert gateway.embed(["x"], _CONTEXT)[0].vector == (0.25,)


def test_ollama_gateway_rejects_invalid_injected_target():
    gateway = OllamaGateway(
        target_resolver=lambda *_args: ("not", "a", "server", "tuple"),
        generate_factory=lambda *args, **kwargs: None,
    )
    with pytest.raises(DependencyUnavailable, match="invalid target"):
        gateway.generate(ModelRequest("hello", "code"), _CONTEXT)
