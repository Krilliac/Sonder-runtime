"""Capability-aware routing rules (pure domain)."""
from __future__ import annotations

from sonder_runtime.domain.routing import capability_router as cr

FULL = ("fast", "code", "general", "reasoning", "vision")
MODEST = ("fast", "code", "general")  # no reasoning/vision configured


def test_code_request_routes_to_code():
    task, conf = cr.classify_task("Implement a Python function to parse this regex")
    assert task == "code"
    assert cr.recommend_tier(task, FULL) == "code"


def test_reasoning_request_routes_to_reasoning_when_available():
    task, _ = cr.classify_task("Prove step by step why this algorithm is O(n log n)")
    assert task == "reasoning"
    assert cr.recommend_tier(task, FULL) == "reasoning"


def test_reasoning_falls_back_to_general_when_no_reasoning_tier():
    assert cr.recommend_tier("reasoning", MODEST) == "general"


def test_vision_forced_by_image_signal():
    task, conf = cr.classify_task("what is this", has_image=True)
    assert task == "vision" and conf == 1.0
    assert cr.recommend_tier(task, FULL) == "vision"
    # No vision tier -> degrades to a base tier, never silently drops.
    assert cr.recommend_tier(task, MODEST) in MODEST


def test_search_request_sets_want_web():
    r = cr.route("what's the latest news on the release date", FULL)
    assert r.task == "search" and r.want_web is True
    assert r.tier == "fast"


def test_long_context_by_size_beats_keywords():
    task, _ = cr.classify_task("implement a function", approx_tokens=9000)
    assert task == "long_context"
    assert cr.recommend_tier(task, FULL) == "general"


def test_explicit_override_wins():
    task, conf = cr.classify_task("anything at all", explicit="vision")
    assert task == "vision" and conf == 1.0


def test_simple_default_for_unmarked_short_request():
    task, _ = cr.classify_task("hello there")
    assert task == "simple"
    assert cr.recommend_tier(task, FULL) == "fast"


def test_escalation_ladder_is_ascending_and_deduped():
    ladder = cr.escalation_ladder("code", FULL)
    assert ladder[0] == "code"
    assert len(set(ladder)) == len(ladder)
    assert all(t in FULL for t in ladder)


def test_oracle_only_appears_with_consent():
    no = cr.escalation_ladder("code", FULL, allow_oracle=False)
    yes = cr.escalation_ladder("code", FULL, allow_oracle=True)
    assert "oracle" not in no
    assert yes[-1] == "oracle"


def test_ladder_filters_to_available_tiers():
    ladder = cr.escalation_ladder("code", MODEST)
    assert "reasoning" not in ladder  # not configured
    assert set(ladder).issubset(set(MODEST))


def test_next_tier_steps_up_only_on_escalation_reason():
    ladder = ("code", "general", "reasoning")
    assert cr.next_tier(ladder, "code", "failed") == "general"
    assert cr.next_tier(ladder, "general", "low_confidence") == "reasoning"
    # exhausted
    assert cr.next_tier(ladder, "reasoning", "failed") is None
    # a non-escalation reason never wastes a stronger model
    assert cr.next_tier(ladder, "code", "done") is None


def test_route_end_to_end_shape():
    r = cr.route("refactor this class and fix the failing pytest", FULL, allow_oracle=True)
    assert r.task == "code"
    assert r.tier == "code"
    assert r.ladder[0] == "code" and r.ladder[-1] == "oracle"
    assert 0.0 < r.confidence <= 1.0


def test_sparse_policy_still_yields_nonempty_ladder():
    # Only 'general' configured — every task must still get a usable ladder.
    ladder = cr.escalation_ladder("vision", ("general",))
    assert ladder and ladder[0] == "general"
