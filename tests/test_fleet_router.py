"""Dynamic model routing for fleet agents -- routing heuristic tests."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.routing.fleet_router import (
    FleetRouter,
    ModelTier,
    RoutingContext,
    RoutingDecision,
)


@pytest.fixture
def router():
    return FleetRouter()


# -- ModelTier ---------------------------------------------------------------

def test_model_tier_values():
    assert ModelTier.FAST.value == "fast"
    assert ModelTier.BALANCED.value == "balanced"
    assert ModelTier.CAPABLE.value == "capable"
    assert ModelTier.MAX.value == "max"


# -- RoutingContext validation -----------------------------------------------

def test_context_rejects_negative_tokens():
    with pytest.raises(ValueError, match="non-negative"):
        RoutingContext(task_description="x", estimated_tokens=-1)


def test_context_rejects_negative_latency():
    with pytest.raises(ValueError, match="non-negative"):
        RoutingContext(task_description="x", max_latency_ms=-1)


def test_context_rejects_invalid_cost_sensitivity():
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        RoutingContext(task_description="x", cost_sensitivity=1.5)
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        RoutingContext(task_description="x", cost_sensitivity=-0.1)


def test_context_defaults():
    ctx = RoutingContext(task_description="hello")
    assert ctx.estimated_tokens == 0
    assert ctx.max_latency_ms == 0
    assert ctx.cost_sensitivity == 0.5
    assert ctx.requires_code is False
    assert ctx.requires_reasoning is False
    assert ctx.parent_tier == ""


# -- Short tasks -> FAST -----------------------------------------------------

def test_short_task_no_reasoning_routes_fast(router):
    ctx = RoutingContext(
        task_description="format this text",
        estimated_tokens=500,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.FAST


def test_very_short_task_routes_fast(router):
    ctx = RoutingContext(
        task_description="hello",
        estimated_tokens=100,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.FAST


# -- Code generation -> BALANCED or CAPABLE ----------------------------------

def test_code_task_routes_balanced(router):
    ctx = RoutingContext(
        task_description="write a function",
        requires_code=True,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.BALANCED


def test_large_code_task_low_cost_routes_capable(router):
    ctx = RoutingContext(
        task_description="implement the full module",
        requires_code=True,
        estimated_tokens=5000,
        cost_sensitivity=0.3,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.CAPABLE


def test_code_task_high_cost_stays_balanced(router):
    ctx = RoutingContext(
        task_description="implement the full module",
        requires_code=True,
        estimated_tokens=5000,
        cost_sensitivity=0.8,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.BALANCED


# -- Reasoning -> CAPABLE ----------------------------------------------------

def test_reasoning_routes_capable(router):
    ctx = RoutingContext(
        task_description="analyze this",
        requires_reasoning=True,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.CAPABLE


def test_reasoning_with_very_high_cost_routes_fast(router):
    ctx = RoutingContext(
        task_description="analyze this",
        requires_reasoning=True,
        cost_sensitivity=0.9,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.FAST


# -- Cost sensitivity --------------------------------------------------------

def test_high_cost_sensitivity_caps_at_balanced(router):
    ctx = RoutingContext(
        task_description="design a system architecture for a distributed cache",
        cost_sensitivity=0.8,
    )
    decision = router.route(ctx)
    assert decision.tier in (ModelTier.FAST, ModelTier.BALANCED)


def test_very_high_cost_sensitivity_forces_fast(router):
    ctx = RoutingContext(
        task_description="design a system architecture for a distributed cache",
        cost_sensitivity=0.9,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.FAST


# -- Parent tier at MAX -> cap child at CAPABLE ------------------------------

def test_parent_max_caps_child(router):
    ctx = RoutingContext(
        task_description="analyze this complex problem step by step",
        requires_reasoning=True,
        parent_tier="max",
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.CAPABLE
    assert "parent at max" in decision.reason


def test_parent_max_does_not_promote(router):
    """A child of a MAX parent doing simple work stays FAST."""
    ctx = RoutingContext(
        task_description="format this text",
        estimated_tokens=200,
        parent_tier="max",
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.FAST


# -- Latency constraint -> FAST ----------------------------------------------

def test_strict_latency_routes_fast(router):
    ctx = RoutingContext(
        task_description="do something complex",
        max_latency_ms=200,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.FAST


def test_relaxed_latency_does_not_force_fast(router):
    ctx = RoutingContext(
        task_description="design a system architecture",
        max_latency_ms=5000,
    )
    decision = router.route(ctx)
    assert decision.tier != ModelTier.FAST


# -- suggest_tier (keyword-only) ---------------------------------------------

def test_suggest_tier_reasoning_keywords(router):
    tier = router.suggest_tier("prove this theorem step by step")
    assert tier == ModelTier.CAPABLE


def test_suggest_tier_code_keywords(router):
    tier = router.suggest_tier("implement a python function for this algorithm")
    assert tier == ModelTier.BALANCED


def test_suggest_tier_fast_keywords(router):
    tier = router.suggest_tier("summarize this list")
    assert tier == ModelTier.FAST


def test_suggest_tier_no_keywords_defaults_balanced(router):
    tier = router.suggest_tier("hello world")
    assert tier == ModelTier.BALANCED


# -- Default routing ---------------------------------------------------------

def test_default_routes_balanced(router):
    ctx = RoutingContext(task_description="do something")
    decision = router.route(ctx)
    assert decision.tier == ModelTier.BALANCED


# -- RoutingDecision ---------------------------------------------------------

def test_decision_carries_cost_factor(router):
    ctx = RoutingContext(task_description="hello", estimated_tokens=100)
    decision = router.route(ctx)
    assert decision.estimated_cost_factor == 0.25  # FAST cost factor


def test_decision_reason_is_nonempty(router):
    ctx = RoutingContext(task_description="do something")
    decision = router.route(ctx)
    assert decision.reason


def test_decision_rejects_negative_cost():
    with pytest.raises(ValueError, match="non-negative"):
        RoutingDecision(
            tier=ModelTier.FAST, reason="test", estimated_cost_factor=-1.0,
        )


# -- FleetRouter with custom tier config ------------------------------------

def test_custom_tier_config_accepted():
    config = {ModelTier.FAST: {"model": "small-model"}}
    router = FleetRouter(tier_config=config)
    # Router still functions with custom config.
    ctx = RoutingContext(task_description="hello", estimated_tokens=100)
    decision = router.route(ctx)
    assert decision.tier == ModelTier.FAST


# -- Combined signals -------------------------------------------------------

def test_code_and_reasoning_prefers_reasoning(router):
    """When both code and reasoning are required, reasoning wins."""
    ctx = RoutingContext(
        task_description="implement and reason about this",
        requires_code=True,
        requires_reasoning=True,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.CAPABLE


def test_short_code_task_prefers_code_over_short(router):
    """Code flag takes precedence over short-task heuristic."""
    ctx = RoutingContext(
        task_description="fix this bug",
        estimated_tokens=200,
        requires_code=True,
    )
    decision = router.route(ctx)
    assert decision.tier == ModelTier.BALANCED
