"""The reasoning/vision tiers must be bindable, routable, and unbindable.

The capability router has always classified reasoning and vision work and
preferred a dedicated tier for it, but the runtime policy only admitted
``fast``/``code``/``general`` -- so those preferences could never resolve and
every such request silently fell back to ``general``. These tests pin the whole
chain: the policy vocabulary, the seeded binding, the server's tier -> model
map, the live selection, and the unset-tier fallback that must not regress.
"""
from __future__ import annotations

import pytest

import sonder_runtime.adapters.runtime_policy as runtime_policy
import server
from sonder_runtime.domain.routing import capability_router as cr
from sonder_runtime.domain.runtime_policy import rules


@pytest.fixture
def isolated_runtime_policy(monkeypatch, tmp_path):
    original_tiers = dict(server.TIERS)
    original_policy = dict(server._RUNTIME_POLICY)
    path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(path))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "sonder-home"))
    yield path
    server.TIERS.clear()
    server.TIERS.update(original_tiers)
    server._RUNTIME_POLICY = original_policy


# --- vocabulary contract -------------------------------------------------


def test_policy_vocabulary_covers_every_tier_the_router_prefers():
    """The regression that started this: a preference nothing can bind to."""
    wanted = set(cr._PREFERRED.values())

    assert wanted <= set(rules.LOCAL_TIERS), (
        "capability router prefers tiers the runtime policy cannot bind: %s"
        % sorted(wanted - set(rules.LOCAL_TIERS))
    )


def test_router_fallback_floor_is_always_bindable():
    """Every fallback rung must be a base tier that can never be unset."""
    assert set(cr._FALLBACK) <= set(rules.BASE_LOCAL_TIERS)


def test_optional_tiers_are_disjoint_from_base_tiers():
    assert not set(rules.BASE_LOCAL_TIERS) & set(rules.OPTIONAL_LOCAL_TIERS)
    assert rules.LOCAL_TIERS == rules.BASE_LOCAL_TIERS + rules.OPTIONAL_LOCAL_TIERS


# --- binding -------------------------------------------------------------


def test_default_policy_leaves_optional_specialists_unbound():
    policy = runtime_policy.default_policy(env={})

    assert policy["local_models"]["reasoning"] == ""
    assert policy["local_models"]["vision"] == ""


def test_environment_overrides_the_specialist_tiers():
    policy = runtime_policy.default_policy(env={
        "SONDER_REASONING": "deepseek-r1:14b",
        "SONDER_VISION": "llava:7b",
    })

    assert policy["local_models"]["reasoning"] == "deepseek-r1:14b"
    assert policy["local_models"]["vision"] == "llava:7b"


def test_environment_can_leave_a_specialist_tier_unset():
    policy = runtime_policy.default_policy(env={"SONDER_VISION": "none"})

    assert policy["local_models"]["vision"] == ""
    assert policy["local_models"]["reasoning"] == ""
    # The unset token is a specialist affordance only; a base tier keeps its
    # default rather than becoming unbound.
    base = runtime_policy.default_policy(env={"SONDER_GENERAL": "none"})
    assert base["local_models"]["general"] == "none"


def test_specialist_tiers_still_refuse_cloud_models():
    policy = runtime_policy.default_policy(env={"SONDER_REASONING": "glm-5.2:cloud"})

    assert policy["local_models"]["reasoning"] == rules.DEFAULT_MODELS["reasoning"]


def test_older_policy_file_gains_safe_unbound_specialist_tiers_on_load():
    legacy = {
        "version": 1,
        "revision": 4,
        "local_models": {
            "fast": "qwen2.5:3b", "code": "sonder:latest", "general": "sonder:latest",
        },
        "routing": {
            "router": "fast", "workbench": "code", "autopilot": "code",
            "fleet": "code", "review": "code",
        },
    }

    normalized = runtime_policy.normalize(legacy)

    assert normalized["local_models"]["reasoning"] == ""
    assert normalized["local_models"]["vision"] == ""


# --- lanes stay on always-bound tiers ------------------------------------


@pytest.mark.parametrize("tier", rules.OPTIONAL_LOCAL_TIERS)
def test_execution_lanes_cannot_pin_to_an_optional_tier(tier):
    payload = {
        "version": 1, "revision": 0,
        "local_models": dict(rules.DEFAULT_MODELS),
        "routing": {
            "router": tier, "workbench": "code", "autopilot": "code",
            "fleet": "code", "review": "code",
        },
    }

    with pytest.raises(ValueError, match="must use"):
        runtime_policy.normalize(payload)


# --- the router actually selects the new tiers ---------------------------


def test_reasoning_prompt_selects_the_reasoning_tier(
    isolated_runtime_policy, monkeypatch,
):
    monkeypatch.setenv("SONDER_REASONING", "deepseek-r1:7b")
    policy = server._refresh_runtime_policy(create=True)
    available = runtime_policy.bound_tiers(policy)
    assert "reasoning" in available

    route = cr.route("Prove step by step why this algorithm is O(n log n)", available)

    assert route.task == "reasoning"
    assert route.tier == "reasoning"
    assert server.TIERS["reasoning"] == "deepseek-r1:7b"


def test_vision_prompt_selects_the_vision_tier(
    isolated_runtime_policy, monkeypatch,
):
    monkeypatch.setenv("SONDER_VISION", "moondream")
    policy = server._refresh_runtime_policy(create=True)
    available = runtime_policy.bound_tiers(policy)

    route = cr.route("describe the screenshot in this image", available)

    assert route.task == "vision"
    assert route.tier == "vision"
    assert server.TIERS["vision"] == "moondream"


def test_capability_refinement_upgrades_the_lane_tier(
    isolated_runtime_policy, monkeypatch,
):
    monkeypatch.setenv("SONDER_REASONING", "deepseek-r1:7b")
    server._refresh_runtime_policy(create=True)

    tier, reason = server._capability_refined_tier(
        "Prove step by step why this bound is tight", "code", "lane default",
    )

    assert tier == "reasoning"
    assert "capability route: reasoning" in reason


def test_capability_refinement_needs_a_real_image_for_the_vision_tier(
    isolated_runtime_policy, monkeypatch,
):
    """A keyword-only vision guess must not hand the run to a VLM.

    "chart" scores as vision, but the work-request lane carries no image, and a
    vision-language model answers a text-only prompt with an immediate
    end-of-sequence -- so the run would silently produce nothing.
    """
    monkeypatch.setenv("SONDER_VISION", "moondream")
    server._refresh_runtime_policy(create=True)
    prompt = "summarize what this chart of build times shows"
    assert cr.classify_task(prompt)[0] == "vision"

    tier, reason = server._capability_refined_tier(prompt, "code", "lane default")
    assert (tier, reason) == ("code", "lane default")

    tier, reason = server._capability_refined_tier(
        prompt, "code", "lane default", has_image=True,
    )
    assert tier == "vision"
    assert "capability route: vision" in reason


def test_capability_refinement_leaves_ordinary_work_alone(isolated_runtime_policy):
    server._refresh_runtime_policy(create=True)

    tier, reason = server._capability_refined_tier(
        "rename this variable in one file", "code", "lane default",
    )

    assert tier == "code"
    assert reason == "lane default"


def test_bound_specialist_tiers_are_offered_as_serve_targets(
    isolated_runtime_policy, monkeypatch,
):
    monkeypatch.setenv("SONDER_REASONING", "deepseek-r1:7b")
    monkeypatch.setenv("SONDER_VISION", "moondream")
    server._refresh_runtime_policy(create=True)

    model, cloud, _augment, label = server._serve_target("reasoning", False)

    assert (model, cloud, label) == ("deepseek-r1:7b", False, "reasoning")
    assert "reasoning" in server.available_tiers()
    assert "vision" in server.available_tiers()


# --- unset tiers must still fall back, not crash or return empty ---------


def test_unset_specialist_tier_falls_back_to_a_base_tier(isolated_runtime_policy):
    runtime_policy.load(create=True)
    runtime_policy.update(local_models={"reasoning": "", "vision": ""})
    policy = server._refresh_runtime_policy(create=False)

    assert policy["local_models"]["reasoning"] == ""
    available = runtime_policy.bound_tiers(policy)
    assert available == rules.BASE_LOCAL_TIERS

    reasoning = cr.route("Prove step by step why this is correct", available)
    vision = cr.route("what is in this photo", available, has_image=True)

    assert reasoning.task == "reasoning" and reasoning.tier == "general"
    assert vision.task == "vision" and vision.tier == "general"
    assert reasoning.ladder and vision.ladder  # never empty


def test_unset_specialist_tier_is_not_offered_as_a_model(isolated_runtime_policy):
    runtime_policy.load(create=True)
    runtime_policy.update(local_models={"vision": ""})
    server._refresh_runtime_policy(create=False)

    assert "vision" not in server.TIERS
    assert "vision" not in server.available_tiers()
    assert "vision" not in server._configured_local_tiers()
    # An unknown tier resolves to no model rather than an empty model name.
    assert server._serve_target("vision", False) == (None, False, True, None)


def test_unset_specialist_tier_never_wins_capability_refinement(
    isolated_runtime_policy,
):
    runtime_policy.load(create=True)
    runtime_policy.update(local_models={"reasoning": "", "vision": ""})
    server._refresh_runtime_policy(create=False)

    tier, reason = server._capability_refined_tier(
        "Prove step by step why this bound is tight", "code", "lane default",
    )

    assert tier == "code"
    assert reason == "lane default"


def test_unset_specialist_tier_round_trips_through_the_policy_file(
    isolated_runtime_policy,
):
    runtime_policy.load(create=True)
    runtime_policy.update(local_models={"vision": ""})

    reloaded = runtime_policy.load(create=False)

    assert reloaded["error"] == ""
    assert reloaded["local_models"]["vision"] == ""
    assert "unset" in runtime_policy.format_policy(reloaded)


def test_rebinding_an_unset_tier_restores_it(isolated_runtime_policy):
    runtime_policy.load(create=True)
    runtime_policy.update(local_models={"vision": ""})
    server._refresh_runtime_policy(create=False)
    assert "vision" not in server.TIERS

    runtime_policy.update(local_models={"vision": "llava:7b"})
    server._refresh_runtime_policy(create=False)

    assert server.TIERS["vision"] == "llava:7b"
    assert "vision" in server._configured_local_tiers()
