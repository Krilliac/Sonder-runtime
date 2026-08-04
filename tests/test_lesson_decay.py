"""Offline, deterministic tests for the lesson-decay logic.

No DB, no clock, no network: age is passed explicitly and similarity is a
fake function, so every assertion is reproducible.
"""
import math

import lesson_decay


# --- decayed_score --------------------------------------------------------

def test_decay_at_age_zero_is_identity():
    assert lesson_decay.decayed_score(1.0, 0) == 1.0
    assert lesson_decay.decayed_score(0.4, 0.0, half_life_days=10) == 0.4


def test_decay_halves_at_one_half_life():
    hl = 30.0
    assert math.isclose(
        lesson_decay.decayed_score(1.0, hl, half_life_days=hl), 0.5
    )
    # Two half-lives -> quarter.
    assert math.isclose(
        lesson_decay.decayed_score(1.0, 2 * hl, half_life_days=hl), 0.25
    )


def test_decay_is_monotonically_lower_with_age_for_positive_base():
    prev = lesson_decay.decayed_score(1.0, 0)
    for age in (1, 5, 20, 60, 200):
        cur = lesson_decay.decayed_score(1.0, age)
        assert cur < prev
        prev = cur


def test_decay_clamps_negative_age_to_zero():
    assert lesson_decay.decayed_score(0.9, -50) == 0.9


def test_decay_respects_custom_half_life():
    # Shorter half-life decays faster: same age, lower score.
    fast = lesson_decay.decayed_score(1.0, 10, half_life_days=5)
    slow = lesson_decay.decayed_score(1.0, 10, half_life_days=50)
    assert fast < slow


def test_decay_non_positive_half_life_returns_base():
    assert lesson_decay.decayed_score(0.7, 100, half_life_days=0) == 0.7


# --- usage_credit / effective_score --------------------------------------

def test_usage_credit_rewards_hits_and_penalizes_misses():
    w = lesson_decay.DEFAULT_USAGE_WEIGHT
    assert lesson_decay.usage_credit(0, 0) == 0.0
    assert math.isclose(lesson_decay.usage_credit(4, 4, usage_weight=w), 4 * w)
    # 1 hit, 3 misses -> net -2.
    assert math.isclose(lesson_decay.usage_credit(4, 1, usage_weight=w), -2 * w)


def test_effective_score_orders_by_usage_when_base_and_age_equal():
    helpful = lesson_decay.effective_score(0.5, 10, uses=8, hits=8)
    harmful = lesson_decay.effective_score(0.5, 10, uses=8, hits=0)
    assert helpful > harmful


def test_effective_score_penalizes_age_when_base_and_usage_equal():
    fresh = lesson_decay.effective_score(0.8, 1, uses=2, hits=2)
    stale = lesson_decay.effective_score(0.8, 120, uses=2, hits=2)
    assert fresh > stale


# --- rank_lessons ---------------------------------------------------------

def test_rank_lessons_orders_by_effective_score():
    lessons = [
        {"id": "stale", "score": 1.0, "created_days": 0, "uses": 0, "hits": 0},
        {"id": "fresh", "score": 1.0, "created_days": 90, "uses": 0, "hits": 0},
        {"id": "proven", "score": 0.6, "created_days": 90, "uses": 10, "hits": 10},
    ]
    ranked = lesson_decay.rank_lessons(lessons, now_days=100)
    ids = [l["id"] for l in ranked]
    # Proven-and-fresh beats fresh-but-unproven beats badly-stale.
    assert ids == ["proven", "fresh", "stale"]


def test_rank_lessons_is_pure():
    lessons = [
        {"id": "a", "score": 0.2, "created_days": 0, "uses": 0, "hits": 0},
        {"id": "b", "score": 0.9, "created_days": 0, "uses": 0, "hits": 0},
    ]
    snapshot = [dict(l) for l in lessons]
    ranked = lesson_decay.rank_lessons(lessons, now_days=10)
    # Input untouched, all lessons returned.
    assert lessons == snapshot
    assert sorted(l["id"] for l in ranked) == ["a", "b"]
    assert ranked[0]["id"] == "b"


def test_rank_lessons_deterministic_tie_break_prefers_newer():
    lessons = [
        {"id": "old", "score": 0.5, "created_days": 0, "uses": 0, "hits": 0},
        {"id": "new", "score": 0.5, "created_days": 50, "uses": 0, "hits": 0},
    ]
    ranked = lesson_decay.rank_lessons(lessons, now_days=50)
    # Same base score but "new" is fresher -> ranks first.
    assert ranked[0]["id"] == "new"


# --- detect_contradictions -----------------------------------------------

def _fake_similarity(pairs):
    """Build a symmetric similarity_fn from an explicit {frozenset: score} map."""
    def sim(a, b):
        return pairs.get(frozenset((a, b)), 0.0)
    return sim


def test_detect_flags_similar_opposite_signal_pair():
    lessons = [
        {"id": "pos", "text": "retry on timeout", "score": 0.9},
        {"id": "neg", "text": "retrying on timeout", "score": -0.8},
    ]
    sim = _fake_similarity({frozenset(("retry on timeout", "retrying on timeout")): 0.95})
    conflicts = lesson_decay.detect_contradictions(lessons, sim, sim_threshold=0.8)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert {c["a"]["id"], c["b"]["id"]} == {"pos", "neg"}
    assert "opposite" in c["reason"]
    assert c["similarity"] == 0.95


def test_detect_does_not_flag_similar_but_agreeing_pair():
    lessons = [
        {"id": "a", "text": "cache the result", "score": 0.9},
        {"id": "b", "text": "caching the result", "score": 0.7},
    ]
    sim = _fake_similarity({frozenset(("cache the result", "caching the result")): 0.97})
    conflicts = lesson_decay.detect_contradictions(lessons, sim)
    assert conflicts == []


def test_detect_does_not_flag_dissimilar_opposite_pair():
    lessons = [
        {"id": "a", "text": "use tabs", "score": 0.9},
        {"id": "b", "text": "delete the database", "score": -0.9},
    ]
    sim = _fake_similarity({frozenset(("use tabs", "delete the database")): 0.10})
    conflicts = lesson_decay.detect_contradictions(lessons, sim, sim_threshold=0.8)
    assert conflicts == []


def test_detect_uses_directive_polarity_when_no_score():
    lessons = [
        {"id": "do", "text": "always retry on timeout"},
        {"id": "dont", "text": "never retry on timeout"},
    ]
    sim = _fake_similarity(
        {frozenset(("always retry on timeout", "never retry on timeout")): 0.9}
    )
    conflicts = lesson_decay.detect_contradictions(lessons, sim)
    assert len(conflicts) == 1
    assert {conflicts[0]["a"]["id"], conflicts[0]["b"]["id"]} == {"do", "dont"}


def test_detect_ignores_neutral_polarity_lessons():
    lessons = [
        {"id": "neutral", "text": "consider the tradeoffs", "score": 0.0},
        {"id": "neg", "text": "considering the tradeoffs", "score": -0.9},
    ]
    sim = _fake_similarity(
        {frozenset(("consider the tradeoffs", "considering the tradeoffs")): 0.99}
    )
    conflicts = lesson_decay.detect_contradictions(lessons, sim)
    # Neutral (score 0) side is never a contradiction.
    assert conflicts == []


def test_detect_respects_signal_field_polarity():
    lessons = [
        {"id": "good", "text": "prefer streaming responses", "signal": "accepted"},
        {"id": "bad", "text": "preferring streaming responses", "signal": "rejected"},
    ]
    sim = _fake_similarity(
        {frozenset(("prefer streaming responses", "preferring streaming responses")): 0.88}
    )
    conflicts = lesson_decay.detect_contradictions(lessons, sim, sim_threshold=0.8)
    assert len(conflicts) == 1
