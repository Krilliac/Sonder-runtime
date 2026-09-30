"""Production tier routing must actually consult the local evidence store."""

from __future__ import annotations

import ast
import time
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

import tier_router
from sonder_runtime.application.routing.backend_conformance import (
    BackendCapability,
    BackendConformanceRecord,
    BackendIdentity,
    ProbeResult,
)
from sonder_runtime.adapters.inference.capability_evidence import load_production_evidence


TIERS = {"code": "coder:latest", "reasoning": "reasoner:latest", "general": "general:latest"}


def _identity(model: str) -> BackendIdentity:
    return BackendIdentity(
        "ollama", model, "a" * 64, "Q4", "1.0", "b" * 64, "c" * 64, 16384, "fixture"
    )


def _record(model: str, *results: tuple[BackendCapability, bool | None]) -> BackendConformanceRecord:
    return BackendConformanceRecord(
        "ollama", model, time.time(),
        tuple(ProbeResult(capability, passed, "fixture") for capability, passed in results),
        synthetic=False, identity=_identity(model),
    )


def _production_router(monkeypatch, tmp_path):
    from sonder_runtime.adapters.inference import production_tier_router

    store = load_production_evidence(tmp_path)
    identity_calls = []
    monkeypatch.setenv("SONDER_CAPABILITY_ROUTING", "advisory")
    monkeypatch.setenv("SONDER_SEMANTIC_TIER_ROUTING", "0")
    monkeypatch.setattr(production_tier_router.capability_evidence,
                        "load_production_evidence", lambda: store)
    monkeypatch.setattr(production_tier_router.capability_evidence,
                        "ollama_identity",
                        lambda origin, model, payload=None: (identity_calls.append(model), _identity(model))[1])
    monkeypatch.setattr(production_tier_router.ollama_endpoint, "normalize",
                        lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(
        production_tier_router, "provider_bindings_from_env",
        lambda: type("Bindings", (), {
            "tier_providers": {tier: "ollama" for tier in TIERS},
            "default_generation_provider": "ollama",
        })(),
    )
    monkeypatch.setattr(production_tier_router, "_identity_cache",
                        production_tier_router.IdentityObservationCache())
    return SimpleNamespace(
        route=partial(production_tier_router.route, router=tier_router.route),
        _test_identity_calls=identity_calls,
    ), store


def test_empty_store_preserves_origin_route_for_text_and_long_input(monkeypatch, tmp_path):
    router, _ = _production_router(monkeypatch, tmp_path)
    cases = [
        ("refactor this loop", None, "code", False),
        ("what is the exact WSARecv signature", None, "code", True),
        ("why does this deadlock under two threads", None, "reasoning", False),
        ("summarize this", {"messages": [{"content": "x" * 25_000}]}, "code", False),
        ("hello", {"tools": [{"type": "function"}]}, "code", False),
        ("rewrite this", {"format": "json"}, "code", False),
        ("hello", {"images": ["fixture"]}, "code", False),
    ]
    # Fixed expectations also verified against origin/main during review;
    # the regression itself does not depend on Git refs in an installed wheel.
    for prompt, payload, expected_tier, expected_fallback in cases:
        actual = router.route(prompt, available_tiers=set(TIERS), tier_models=TIERS,
                              request_payload=payload)
        assert actual["tier"] == expected_tier
        assert actual["fallback_used"] is expected_fallback
        assert actual["signal"] == "lexical"
    assert router._test_identity_calls == []


@pytest.mark.parametrize("capability,payload", [
    (BackendCapability.STRUCTURED, {"format": {"type": "object"}}),
    (BackendCapability.TOOL_NATIVE, {"tools": [{"type": "function"}]}),
    (BackendCapability.TOOLS_WITH_SCHEMA, {"tools": ["echo"], "format": "json"}),
    (BackendCapability.VISION, {"images": ["fixture"]}),
    (BackendCapability.LONG_CONTEXT, {"messages": [{"content": "x" * 24576}]}),
])
def test_fresh_failure_on_preferred_model_selects_eligible_alternative(monkeypatch, tmp_path,
                                                                    capability, payload):
    router, store = _production_router(monkeypatch, tmp_path)
    store.save(_record("coder:latest", (BackendCapability.CHAT, True),
                        (capability, False)))
    result = router.route(
        "refactor this loop", available_tiers=set(TIERS), tier_models=TIERS,
        request_payload=payload,
    )
    assert result["tier"] in {"reasoning", "general"}
    assert "failed" in result["reason"]
    assert result["fallback_used"] is True
    assert capability.value in result["reason"]


def test_all_candidates_failed_keep_configured_tier_and_signal_fallback(monkeypatch, tmp_path):
    router, store = _production_router(monkeypatch, tmp_path)
    for model in TIERS.values():
        store.save(_record(model, (BackendCapability.CHAT, True),
                            (BackendCapability.STRUCTURED, False)))
    result = router.route(
        "refactor this loop", available_tiers=set(TIERS), tier_models=TIERS,
        request_payload={"format": {"type": "object"}},
    )
    assert result["tier"] == "code"
    assert result["fallback_used"] is True
    assert "fallback" in result["reason"]


def test_absent_stale_and_unrelated_evidence_preserve_route(monkeypatch, tmp_path):
    router, store = _production_router(monkeypatch, tmp_path)
    for record in (
        _record("other:latest", (BackendCapability.STRUCTURED, False)),
        _record("coder:latest", (BackendCapability.STRUCTURED, None)),
        replace(_record("coder:latest", (BackendCapability.STRUCTURED, False)), checked_at=1),
    ):
        store.save(record)
        actual = router.route("refactor this loop", available_tiers=set(TIERS), tier_models=TIERS,
                              request_payload={"format": {"type": "object"}})
        assert actual["tier"] == "code"
    assert router._test_identity_calls == []


def test_failed_identity_is_observed_once_and_passes_are_never_observed(monkeypatch, tmp_path):
    router, store = _production_router(monkeypatch, tmp_path)
    store.save(_record("coder:latest", (BackendCapability.CHAT, True),
                        (BackendCapability.STRUCTURED, False)))
    payload = {"format": {"type": "object"}}
    router.route("refactor this loop", available_tiers=set(TIERS), tier_models=TIERS,
                 request_payload=payload)
    router.route("refactor this loop", available_tiers=set(TIERS), tier_models=TIERS,
                 request_payload=payload)
    assert router._test_identity_calls == ["coder:latest"]

    router, store = _production_router(monkeypatch, tmp_path / "passes")
    store.save(_record("coder:latest", (BackendCapability.CHAT, True),
                        (BackendCapability.STRUCTURED, True)))
    router.route("refactor this loop", available_tiers=set(TIERS), tier_models=TIERS,
                 request_payload=payload)
    assert router._test_identity_calls == []


def test_off_mode_skips_evidence_and_identity(monkeypatch, tmp_path):
    router, store = _production_router(monkeypatch, tmp_path)
    monkeypatch.setenv("SONDER_CAPABILITY_ROUTING", "off")
    store.save(_record("coder:latest", (BackendCapability.CHAT, True),
                        (BackendCapability.STRUCTURED, False)))
    result = router.route("refactor this loop", available_tiers=set(TIERS), tier_models=TIERS,
                          request_payload={"format": {"type": "object"}})
    assert result["tier"] == "code"
    assert router._test_identity_calls == []


def test_current_tier_model_binding_is_read_on_each_call(monkeypatch, tmp_path):
    router, store = _production_router(monkeypatch, tmp_path)
    bindings = dict(TIERS)
    payload = {"format": {"type": "object"}}
    store.save(_record("coder:latest", (BackendCapability.CHAT, True),
                        (BackendCapability.STRUCTURED, False)))
    first = router.route("refactor this loop", available_tiers=set(bindings), tier_models=bindings,
                         request_payload=payload)
    assert first["tier"] in {"reasoning", "general"}
    bindings["code"] = "new-coder:latest"
    second = router.route("refactor this loop", available_tiers=set(bindings), tier_models=bindings,
                          request_payload=payload)
    assert second["tier"] == "code"


def test_server_callers_use_packaged_router_and_payload_wiring():
    tree = ast.parse(Path("server.py").read_text(encoding="utf-8"))
    funcs = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    route_body = ast.unparse(funcs["route_request"])
    improve_body = ast.unparse(funcs["improve_function"])
    assert "tier_router.route" in route_body
    assert "tier_models=TIERS" in route_body
    assert "tier_router.route" in improve_body
    assert "request_payload" in improve_body
    assert "messages" in improve_body


def _server_callers(tiers, source, replies):
    """Execute the real tool bodies with inert I/O, without importing server."""
    import code_improve
    from sonder_runtime.adapters.inference import production_tier_router

    namespace = {
        "tier_router": tier_router, "production_tier_router": production_tier_router,
        "TIERS": tiers, "_maybe_live_reload": lambda: None,
        "file_ops": SimpleNamespace(read_file=lambda _: {"text": source}),
        "code_improve": code_improve,
        "ensemble_answer": lambda prompt, tiers, mode: replies.append((prompt, tiers)) or source,
    }
    tree = ast.parse((Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"route_request", "improve_function"}]
    for node in functions:
        node.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), "server.py", "exec"), namespace)
    return namespace


def test_route_request_uses_production_store_payload_and_live_tiers(monkeypatch, tmp_path):
    router, store = _production_router(monkeypatch, tmp_path)
    store.save(_record("coder:latest", (BackendCapability.STRUCTURED, False)))
    tiers = dict(TIERS)
    tool = _server_callers(tiers, "", [])["route_request"]
    result = tool("refactor this loop", request_payload={"format": "json"})
    assert "tier: general" in result and "failed" in result
    assert router._test_identity_calls == ["coder:latest"]
    tiers["code"] = "replacement:latest"
    assert "tier: code" in tool("refactor this loop", request_payload={"format": "json"})


def test_improve_function_uses_target_input_length_and_preserves_override(monkeypatch, tmp_path):
    router, store = _production_router(monkeypatch, tmp_path)
    store.save(_record("coder:latest", (BackendCapability.LONG_CONTEXT, False)))
    source = "def sample():\n    return '" + "x" * 24576 + "'\n"
    replies = []
    tool = _server_callers(TIERS, source, replies)["improve_function"]
    result = tool("fixture.py", "sample", objective="refactor this loop")
    assert "tier=general" in result
    assert replies[0][1] == "general"
    router._test_identity_calls.clear()
    tool("fixture.py", "sample", objective="refactor this loop", tier="code")
    assert replies[-1][1] == "code" and router._test_identity_calls == []
    # An unrelated large function is never sent to the model for this target.
    short_source = "def sample():\n    return 1\n\ndef other():\n    return '" + "x" * 24576 + "'\n"
    tool = _server_callers(TIERS, short_source, replies)["improve_function"]
    tool("fixture.py", "sample", objective="refactor this loop")
    assert replies[-1][1] == "code" and router._test_identity_calls == []


def test_non_ollama_tiers_do_not_inherit_local_model_failure(monkeypatch, tmp_path):
    from sonder_runtime.adapters.inference import production_tier_router

    router, store = _production_router(monkeypatch, tmp_path)
    store.save(_record("coder:latest", (BackendCapability.STRUCTURED, False)))
    monkeypatch.setattr(production_tier_router, "provider_bindings_from_env", lambda: SimpleNamespace(
        tier_providers={"code": "sonder_inference"}, default_generation_provider="ollama",
    ))
    result = router.route("refactor this loop", tier_models=TIERS, request_payload={"format": "json"})
    assert result["tier"] == "code" and router._test_identity_calls == []
