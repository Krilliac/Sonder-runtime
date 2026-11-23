"""Every routing call path, and proof that none adds a probe on the normal path.

Context-fit routing and launch-profile metadata may perform I/O in exactly
these places (anything else is a regression this file catches):

* ``overflow_route.plan`` -> ``context_fit_route.plan``: the HTTP chat loop
  only (``server._answer_with_history_impl``), and only for a turn estimated
  above ``context_fit.PROBE_FLOOR_TOKENS``;
* ``SonderInferenceGateway.provider_status`` / ``tier_context`` /
  ``model_profiles``: status and doctor (TTL-cached);
* ``runtime_policy.format_policy`` -> ``tier_context_lines``: caches only.

The other routing paths -- root ``tier_router.route``, the production tier
router, and ``SonderInferenceGateway.generate`` -- must never reach them.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re

import pytest

import tier_router
from sonder_runtime.adapters.inference import context_fit_route, launch_profile_cache
from sonder_runtime.adapters.inference import overflow_route, production_tier_router
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.routing import long_context_overflow as overflow
from sonder_runtime.application.routing import tier_escalation

ROOT = pathlib.Path(__file__).resolve().parents[1]
LONG_PROMPT = "please review this function\n" + "x = compute(y)\n" * 30_000  # ~110k tokens

# Symbol -> the only production files allowed to use it.
PROBE_ENTRY_POINTS = {
    r"\bcontext_fit_route\b": {
        "sonder_runtime/adapters/inference/overflow_route.py",
        "sonder_runtime/adapters/runtime_policy.py",
        "sonder_runtime/application/routing/context_fit.py",  # docstring reference
    },
    r"\.model_profiles\(": {
        "sonder_runtime/adapters/inference/context_fit_route.py",
        "sonder_runtime/adapters/inference/sonder_inference_gateway.py",
    },
    r"\b_fetch_profile_catalog\(": {
        "sonder_runtime/adapters/inference/sonder_inference_gateway.py",
    },
    r"\b_context_fit\.plan\(": {
        "sonder_runtime/adapters/inference/overflow_route.py",
    },
    r"\b_overflow_route\.plan\(": {"server.py"},
    r"[\"']tier_context[\"']": {
        "sonder_doctor.py",
        "sonder_runtime/adapters/inference/sonder_inference_gateway.py",
    },
}


def _production_sources():
    files = [ROOT / "runtime" / "server.py", ROOT / "runtime" / "tier_router.py", ROOT / "runtime" / "sonder_doctor.py"]
    files += sorted((ROOT / "sonder_runtime").rglob("*.py"))
    return {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8") for path in files}


@pytest.mark.parametrize("pattern", sorted(PROBE_ENTRY_POINTS))
def test_probe_entry_points_are_reached_only_from_the_enumerated_paths(pattern):
    users = {name for name, text in _production_sources().items() if re.search(pattern, text)}
    assert users == PROBE_ENTRY_POINTS[pattern]


def test_server_calls_the_context_fit_plan_once_on_the_chat_loop_only():
    text = (ROOT / "runtime" / "server.py").read_text(encoding="utf-8")
    assert len(re.findall(r"_overflow_route\.plan\(", text)) == 1
    start = text.index("def _answer_with_history_impl(")
    end = text.index("\ndef ", start + 1)
    assert "_overflow_route.plan(" in text[start:end]


@pytest.mark.parametrize("relative", [
    "sonder_runtime/platform/tier_model_options.py",
    "sonder_runtime/application/routing/context_fit.py",
])
def test_pure_modules_perform_no_io(relative):
    """These modules import nothing that can reach a socket or a file."""
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "json", "os", "typing", "collections", "dataclasses"}
    calls = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in calls


@pytest.fixture
def no_probe(monkeypatch):
    """Any context-fit or profile lookup fails the test."""
    def boom(*_a, **_k):
        raise AssertionError("probe on a routing path that must not probe")

    monkeypatch.setattr(SonderInferenceGateway, "_fetch_profile_catalog", boom)
    monkeypatch.setattr(SonderInferenceGateway, "model_profiles", boom)
    monkeypatch.setattr(context_fit_route, "tier_window", boom)
    monkeypatch.setattr(context_fit_route, "plan", boom)
    for name in ("SONDER_REASONING_PROVIDER", "SONDER_CODE_PROVIDER"):
        monkeypatch.setenv(name, "sonder-inference")
    monkeypatch.setenv("SONDER_LONG_CONTEXT_TIER", "reasoning")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "reasoning=qwen3.8-27b-q3-100k")


@pytest.mark.parametrize("mode", ["off", "advisory"])
def test_root_tier_router_never_probes(no_probe, monkeypatch, mode):
    monkeypatch.setenv("SONDER_CAPABILITY_ROUTING", mode)
    decision = tier_router.route(LONG_PROMPT, ["fast", "code", "reasoning"], semantic_enabled=False)
    assert decision["tier"] in {"fast", "code", "reasoning"}


def test_production_tier_router_never_probes(no_probe, monkeypatch):
    monkeypatch.setenv("SONDER_CAPABILITY_ROUTING", "off")
    seen = []

    def router(prompt, available, **options):
        seen.append((len(prompt), tuple(available)))
        return {"tier": "code"}

    tiers = {"fast": "f", "code": "c", "reasoning": "r"}
    decision = production_tier_router.route(LONG_PROMPT, set(tiers), router=router, tier_models=tiers)
    assert decision == {"tier": "code"} and len(seen) == 1


def test_inference_generate_never_reads_profile_metadata_even_for_a_long_prompt():
    """The gateway request path adds no ``/v1/models`` probe."""
    gets, posts = [], []

    def get(url, headers, timeout):
        gets.append(url)
        if url.endswith("/v1/sonder/health"):
            return 200, json.dumps({"status": "ready", "api_version": 1, "version": "0.2.0",
                                    "synthetic": False, "models": [{"id": "m"}]}).encode()
        raise AssertionError(url)

    def post(url, payload, headers, timeout):
        posts.append(url)
        return {
            "id": "c", "object": "chat.completion", "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    cache = launch_profile_cache.ProfileCache()
    gateway = SonderInferenceGateway(
        SonderInferenceConfig(), get_transport=get, transport=post, profile_cache=cache,
    )
    context = local_owner_context(correlation_id="t1")
    gateway.generate(ModelRequest(prompt="x" * 400_000, tier="reasoning"), context)
    gateway.generate(ModelRequest(prompt="hi", tier="fast"), context)
    assert [url for url in gets if url.endswith("/v1/models")] == []
    assert len(posts) == 2
    assert cache.cached(SonderInferenceConfig().base_url) is None


class _Host:
    """Minimal ``server`` stand-in; any metadata read is a probe."""

    TIERS = {"code": "qwen:32b", "reasoning": "qwen3.8-27b-q3-100k"}
    _RUNTIME_POLICY = {"routing": {}}

    def __init__(self):
        self.metadata_reads = 0

    def _bridge_provider_for_tier(self, tier, cloud):
        return None

    def _model_context_metadata(self, model):
        self.metadata_reads += 1
        return 32768, "27B"


def test_chat_loop_short_turn_does_no_lookup_at_all(monkeypatch):
    """The normal path (estimate at or below the floor) never calls a lookup."""
    host = _Host()
    plan = tier_escalation.single(tier_escalation.Rung(tier="code", model="qwen:32b", cloud=False))
    monkeypatch.setattr(overflow_route._overflow, "plan_turn", lambda *a, **k: (plan, None))
    monkeypatch.setattr(context_fit_route, "tier_window",
                        lambda *a, **k: pytest.fail("short turn looked a window up"))
    prompt = "y" * (4 * context_fit_route._fit.PROBE_FLOOR_TOKENS - 64)
    assert overflow.estimate_prompt_tokens(prompt, []) <= context_fit_route._fit.PROBE_FLOOR_TOKENS
    result, decision = overflow_route.plan(host, prompt, [], plan)
    assert (result, decision) == (plan, None)
    assert host.metadata_reads == 0
