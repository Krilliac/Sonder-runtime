"""The semantic backstop must never replace the measured lexical axis."""
from __future__ import annotations

import math
import os

import pytest

import tier_router
from sonder_runtime.platform.config import load_config


def _signal(prompt):
    return {"tier": "fast", "margin": 0.4, "model": "test-embedder"}


def test_disabled_by_default_does_not_consult_semantics(monkeypatch):
    monkeypatch.delenv("SONDER_SEMANTIC_TIER_ROUTING", raising=False)
    calls = []
    result = tier_router.route("hello", semantic_classifier=lambda p: calls.append(p))
    assert result == {
        "kind": "general", "tier": "code", "signal": "lexical",
        "reason": "no strong signal either way; the default local tier",
        "fallback_used": False,
    }
    assert calls == []


@pytest.mark.parametrize("prompt,tier", [
    ("refactor this loop", "code"),
    ("what is the exact API signature", "cloud-general"),
    ("prove this loop terminates", "reasoning"),
    ("```python\ndef f(): pass\n``` which branch is dead?", "code"),
])
def test_lexical_signal_always_wins(prompt, tier):
    calls = []
    result = tier_router.route(
        prompt, semantic_enabled=True,
        semantic_classifier=lambda p: calls.append(p),
    )
    assert result["tier"] == tier
    assert result["signal"] == "lexical"
    assert calls == []


def test_environment_opt_in_and_legible_reason(monkeypatch):
    monkeypatch.setenv("SONDER_SEMANTIC_TIER_ROUTING", "1")
    result = tier_router.route("hello", {"code", "fast"}, semantic_classifier=_signal)
    assert result["kind"] == "general"
    assert result["tier"] == "fast"
    assert result["signal"] == "semantic"
    assert result["fallback_used"] is False
    assert all(part in result["reason"] for part in ("fast", "0.400", "test-embedder"))


def test_explicit_disable_overrides_environment(monkeypatch):
    monkeypatch.setenv("SONDER_SEMANTIC_TIER_ROUTING", "1")
    result = tier_router.route(
        "hello", semantic_enabled=False, semantic_classifier=_signal,
    )
    assert result["signal"] == "lexical"


@pytest.mark.parametrize("margin", [0.0, 0.049, -0.1, math.nan, math.inf, "bad", True])
def test_low_or_invalid_margin_abstains(margin):
    result = tier_router.route(
        "hello", semantic_enabled=True,
        semantic_classifier=lambda p: {**_signal(p), "margin": margin},
    )
    assert result["tier"] == "code"
    assert result["signal"] == "lexical"


def test_margin_at_threshold_is_accepted():
    from sonder_runtime.domain.routing.semantic_tier import MIN_MARGIN

    result = tier_router.route(
        "hello", semantic_enabled=True,
        semantic_classifier=lambda p: {**_signal(p), "margin": MIN_MARGIN},
    )
    assert result["signal"] == "semantic"


@pytest.mark.parametrize("tier", ["vision", "cloud-general", "unknown"])
def test_only_text_local_semantic_tiers_are_accepted(tier):
    result = tier_router.route(
        "look at the picture", {"code", tier}, semantic_enabled=True,
        semantic_classifier=lambda p: {**_signal(p), "tier": tier},
    )
    assert result["tier"] == "code"
    assert result["signal"] == "lexical"


def test_vision_cannot_be_selected_even_by_last_resort_fallback():
    result = tier_router.route("hello", {"vision"})
    assert result["tier"] != "vision"
    assert result["fallback_used"] is True


def test_unavailable_semantic_tier_uses_original_fallback():
    result = tier_router.route(
        "hello", {"general"}, semantic_enabled=True, semantic_classifier=_signal,
    )
    assert result["tier"] == "general"
    assert result["signal"] == "lexical"
    assert result["fallback_used"] is True
    assert "unavailable" in result["reason"]


@pytest.mark.parametrize("failure", [None, {}, RuntimeError("embedder unavailable")])
def test_semantic_failure_keeps_today_behavior(failure):
    def classifier(prompt):
        if isinstance(failure, Exception):
            raise failure
        return failure

    before = tier_router.route("hello", {"reasoning"})
    after = tier_router.route(
        "hello", {"reasoning"}, semantic_enabled=True, semantic_classifier=classifier,
    )
    assert after == before


def test_embedding_failure_keeps_today_behavior():
    def unavailable(text, **kwargs):
        raise RuntimeError("local embedder stopped")

    before = tier_router.route("hello")
    after = tier_router.route("hello", semantic_enabled=True, embedder=unavailable)
    assert after == before


def test_config_defaults_off_and_environment_opts_in():
    assert load_config(env={}).features.semantic_tier_routing is False
    assert load_config(env={"SONDER_SEMANTIC_TIER_ROUTING": "1"}).features.semantic_tier_routing is True
    assert load_config(env={"SONDER_SEMANTIC_TIER_ROUTING": "0"}).features.semantic_tier_routing is False


def test_typed_feature_override():
    config = load_config(env={}, overrides={"features.semantic_tier_routing": "true"})
    assert config.features.semantic_tier_routing is True


@pytest.mark.parametrize("include_typed_runtime", [False, True])
def test_typed_feature_export_reaches_router(monkeypatch, include_typed_runtime):
    from sonder_runtime import __main__ as entry

    # Export changes many compatibility variables. Keep this test hermetic
    # even in an isolated run without the repository-wide autouse fixtures.
    monkeypatch.setattr(os, "environ", {})
    config = load_config(env={}, overrides={"features.semantic_tier_routing": "true"})
    entry._export_runtime_environment(config, include_typed_runtime=include_typed_runtime)
    assert tier_router.route("hello", semantic_classifier=_signal)["signal"] == "semantic"
