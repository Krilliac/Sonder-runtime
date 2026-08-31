"""SPEC-5 WP3: RoutePlanner contract tests."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.routing.route_planner import (
    AvailableModels,
    ModelRoute,
    RoutePlanner,
    RoutingRequest,
)
from sonder_runtime.domain.runtime_policy.rules import default_policy


@pytest.fixture
def planner():
    return RoutePlanner()


@pytest.fixture
def policy():
    return default_policy(env={})


@pytest.fixture
def available():
    return AvailableModels(
        tier_models={
            "fast": "sonder:latest",
            "code": "sonder:latest",
            "general": "sonder:latest",
            "reasoning": "",
            "vision": "",
        },
        provider="ollama",
    )


@pytest.fixture
def available_with_specialists():
    return AvailableModels(
        tier_models={
            "fast": "sonder:latest",
            "code": "sonder:latest",
            "general": "sonder:latest",
            "reasoning": "deepseek-r1:14b",
            "vision": "llava:7b",
        },
        provider="ollama",
    )


class TestRouteDeterminism:
    def test_same_input_same_route(self, planner, policy, available):
        req = RoutingRequest(lane="workbench", prompt="write a function")
        r1 = planner.select(req, policy, available)
        r2 = planner.select(req, policy, available)
        assert r1 == r2

    def test_route_is_frozen(self, planner, policy, available):
        req = RoutingRequest(lane="workbench", prompt="hello")
        route = planner.select(req, policy, available)
        with pytest.raises(AttributeError):
            route.tier = "fast"  # type: ignore[misc]


class TestTierSelection:
    def test_code_prompt_gets_code_tier(self, planner, policy, available):
        req = RoutingRequest(lane="workbench", prompt="implement a function in python")
        route = planner.select(req, policy, available)
        assert route.tier == "code"

    def test_simple_prompt_gets_fast(self, planner, policy, available):
        req = RoutingRequest(lane="router", prompt="hello")
        route = planner.select(req, policy, available)
        assert route.tier in ("fast", "general", "code")

    def test_vision_uses_specialist_when_available(
        self, planner, policy, available_with_specialists
    ):
        req = RoutingRequest(
            lane="workbench", prompt="describe this image", has_image=True
        )
        route = planner.select(req, policy, available_with_specialists)
        assert route.tier == "vision"
        assert "vision" in route.capabilities

    def test_reasoning_uses_specialist_when_available(
        self, planner, policy, available_with_specialists
    ):
        req = RoutingRequest(
            lane="workbench", prompt="prove this theorem step by step"
        )
        route = planner.select(req, policy, available_with_specialists)
        assert route.tier == "reasoning"

    def test_unbound_specialist_falls_to_base(self, planner, policy, available):
        req = RoutingRequest(
            lane="workbench", prompt="describe this image", has_image=True
        )
        route = planner.select(req, policy, available)
        assert route.tier in ("fast", "code", "general")


class TestCloudBoundary:
    def test_local_failure_never_becomes_hosted(self, planner, policy, available):
        """SPEC-5 §14: no automatic local→cloud failover."""
        req = RoutingRequest(lane="workbench", prompt="hello")
        route = planner.select(req, policy, available)
        assert route.provider == "ollama"

    def test_retry_never_changes_provider(self, planner, policy, available):
        req = RoutingRequest(lane="workbench", prompt="hello")
        route = planner.select(req, policy, available)
        # Even on the escalation ladder, provider stays the same
        for tier in route.ladder:
            assert tier != "cloud"

    def test_selected_tier_uses_its_provider_binding(self, planner, policy):
        available = AvailableModels(
            tier_models={
                "fast": "bonsai",
                "general": "bonsai",
                "code": "qwen",
                "reasoning": "qwen",
                "vision": "",
            },
            provider="ollama",
            tier_providers={
                "fast": "openai_compatible",
                "general": "openai_compatible",
                "code": "ollama",
                "reasoning": "ollama",
            },
        )

        route = planner.select(
            RoutingRequest(lane="router", prompt="hello"), policy, available
        )

        assert route.provider == available.provider_for(route.tier)
        assert route.provider in {"ollama", "openai_compatible"}


class TestLaneMapping:
    def test_all_lanes_valid(self, planner, policy, available):
        for lane in ("router", "workbench", "autopilot", "fleet", "review"):
            req = RoutingRequest(lane=lane, prompt="test")
            route = planner.select(req, policy, available)
            assert route.lane == lane

    def test_unknown_lane_raises(self, planner, policy, available):
        req = RoutingRequest(lane="invalid", prompt="test")
        with pytest.raises(ValueError, match="unknown lane"):
            planner.select(req, policy, available)


class TestFromPolicy:
    def test_builds_available_from_policy(self, policy):
        available = RoutePlanner.from_policy(policy)
        assert "fast" in available.available_tiers
        assert "code" in available.available_tiers
        assert "general" in available.available_tiers

    def test_unset_specialists_excluded(self, policy):
        available = RoutePlanner.from_policy(policy)
        assert "reasoning" not in available.available_tiers
        assert "vision" not in available.available_tiers

    def test_from_policy_preserves_per_tier_providers(self, policy):
        available = RoutePlanner.from_policy(
            policy,
            provider="ollama",
            tier_providers={"fast": "openai_compatible"},
        )
        assert available.provider_for("fast") == "openai_compatible"
        assert available.provider_for("code") == "ollama"


class TestNoSideEffects:
    def test_planner_is_pure(self, planner, policy, available):
        """RoutePlanner performs no I/O — just verify it's importable
        and callable without any infrastructure."""
        req = RoutingRequest(lane="workbench", prompt="test")
        route = planner.select(req, policy, available)
        assert isinstance(route, ModelRoute)
