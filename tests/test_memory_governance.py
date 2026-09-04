"""Tests for memory governance: curation, dedup, decay, and capacity."""
from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.domain.memory.governance import (
    MemoryPolicy,
    curate,
    eviction_candidates,
    score_memory,
    should_store,
)


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# MemoryPolicy validation
# ---------------------------------------------------------------------------

class TestMemoryPolicy:
    def test_defaults(self):
        p = MemoryPolicy()
        assert p.max_entries == 1000
        assert p.max_age_days == 90
        assert p.dedup_threshold == 0.85
        assert p.decay_factor == 0.95

    def test_custom_values(self):
        p = MemoryPolicy(max_entries=50, max_age_days=30, dedup_threshold=0.9, decay_factor=0.8)
        assert p.max_entries == 50
        assert p.max_age_days == 30

    def test_invalid_max_entries(self):
        with pytest.raises(ValueError):
            MemoryPolicy(max_entries=0)

    def test_invalid_dedup_threshold(self):
        with pytest.raises(ValueError):
            MemoryPolicy(dedup_threshold=1.5)

    def test_invalid_decay_factor(self):
        with pytest.raises(ValueError):
            MemoryPolicy(decay_factor=0.0)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDedup:
    def test_exact_duplicate_scores_zero(self):
        existing = ["Use pathlib.Path for file path joins"]
        score = score_memory("Use pathlib.Path for file path joins", existing)
        assert score == 0.0

    def test_near_duplicate_scores_zero(self):
        existing = ["Use pathlib Path for joining resolving and manipulating file system paths in Python safely"]
        # One word different in a long sentence -> Jaccard 0.867 > 0.85
        candidate = "Use pathlib Path for joining resolving and manipulating file system paths in Python securely"
        score = score_memory(candidate, existing)
        assert score == 0.0

    def test_near_duplicate_blocked_by_should_store(self):
        existing = ["Always use context managers for file handling in Python"]
        candidate = "Always use context managers for file handling in Python code"
        assert should_store(candidate, existing) is False

    def test_distinct_content_passes_dedup(self):
        existing = ["Use pathlib.Path for file path joins"]
        candidate = "Prefer dataclasses over plain dicts for structured data"
        score = score_memory(candidate, existing)
        assert score > 0.0


# ---------------------------------------------------------------------------
# Novelty scoring
# ---------------------------------------------------------------------------

class TestNoveltyScoring:
    def test_unique_content_scores_high(self):
        existing = [
            "Use pytest fixtures for test setup",
            "Prefer list comprehensions over map/filter",
        ]
        candidate = "Always validate user input at API boundaries before processing"
        score = score_memory(candidate, existing)
        assert score > 0.5

    def test_partially_overlapping_scores_moderate(self):
        existing = ["Use pytest fixtures for test setup and teardown"]
        candidate = "Use pytest parametrize for test data variation"
        score = score_memory(candidate, existing)
        # Shares some tokens (use, pytest, test) but is distinct
        assert 0.0 < score < 1.0

    def test_empty_existing_gives_length_only_score(self):
        candidate = "Prefer early returns to reduce nesting depth in functions"
        score = score_memory(candidate, [])
        assert score > 0.0

    def test_too_short_content_scores_zero(self):
        score = score_memory("ok", ["some existing memory content here"])
        assert score == 0.0

    def test_empty_content_not_stored(self):
        assert should_store("", []) is False
        assert should_store("   ", []) is False


# ---------------------------------------------------------------------------
# Age-based eviction
# ---------------------------------------------------------------------------

class TestAgeEviction:
    def test_old_memories_evicted(self):
        old_date = NOW - timedelta(days=100)
        memories = [
            {"id": "old1", "content": "stale lesson", "created_at": old_date},
            {"id": "new1", "content": "fresh lesson about error handling",
             "created_at": NOW - timedelta(days=10)},
        ]
        evicted = eviction_candidates(memories, now=NOW)
        assert "old1" in evicted
        assert "new1" not in evicted

    def test_exactly_at_boundary_not_evicted(self):
        boundary = NOW - timedelta(days=90)
        memories = [
            {"id": "edge", "content": "boundary lesson content here",
             "created_at": boundary},
        ]
        evicted = eviction_candidates(memories, now=NOW)
        assert "edge" not in evicted

    def test_iso_string_timestamps(self):
        old_iso = (NOW - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%S")
        memories = [
            {"id": "iso1", "content": "old content from string timestamp",
             "created_at": old_iso},
        ]
        evicted = eviction_candidates(memories, now=NOW)
        assert "iso1" in evicted

    def test_custom_max_age(self):
        policy = MemoryPolicy(max_age_days=30)
        memories = [
            {"id": "m1", "content": "lesson about testing patterns",
             "created_at": NOW - timedelta(days=45)},
        ]
        evicted = eviction_candidates(memories, policy, now=NOW)
        assert "m1" in evicted


# ---------------------------------------------------------------------------
# Capacity limit enforcement
# ---------------------------------------------------------------------------

class TestCapacityLimit:
    def test_within_limit_nothing_evicted(self):
        policy = MemoryPolicy(max_entries=5)
        memories = [
            {"id": f"m{i}", "content": f"unique memory content number {i}",
             "created_at": NOW}
            for i in range(5)
        ]
        evicted = eviction_candidates(memories, policy, now=NOW)
        assert len(evicted) == 0

    def test_over_limit_evicts_lowest_scored(self):
        policy = MemoryPolicy(max_entries=2)
        memories = [
            {"id": "good", "content": "Always validate inputs at API boundaries before processing",
             "score": 0.9, "created_at": NOW},
            {"id": "medium", "content": "Check error returns from system calls",
             "score": 0.5, "created_at": NOW},
            {"id": "low", "content": "ok sure thing",
             "score": 0.1, "created_at": NOW},
        ]
        evicted = eviction_candidates(memories, policy, now=NOW)
        assert len(evicted) == 1
        assert "low" in evicted
        assert "good" not in evicted

    def test_combined_age_and_capacity(self):
        policy = MemoryPolicy(max_entries=2, max_age_days=30)
        memories = [
            {"id": "old", "content": "ancient lesson content here",
             "created_at": NOW - timedelta(days=60)},
            {"id": "a", "content": "recent lesson about error handling patterns",
             "score": 0.8, "created_at": NOW},
            {"id": "b", "content": "another recent lesson about testing strategies",
             "score": 0.7, "created_at": NOW},
            {"id": "c", "content": "yet another lesson about code review",
             "score": 0.3, "created_at": NOW},
        ]
        evicted = eviction_candidates(memories, policy, now=NOW)
        # old evicted by age, c evicted by capacity (3 remaining > max 2)
        assert "old" in evicted
        assert "c" in evicted
        assert "a" not in evicted
        assert "b" not in evicted


# ---------------------------------------------------------------------------
# curate() end-to-end
# ---------------------------------------------------------------------------

class TestCurate:
    def test_removes_old_and_duplicates(self):
        policy = MemoryPolicy(max_entries=100, max_age_days=30, dedup_threshold=0.85)
        memories = [
            {"id": "old", "content": "ancient stale lesson about paths",
             "created_at": NOW - timedelta(days=60)},
            {"id": "a", "content": "Use context managers for safe resource cleanup in Python",
             "created_at": NOW},
            {"id": "a_dup", "content": "Use context managers for safe resource cleanup in Python code",
             "created_at": NOW},
            {"id": "b", "content": "Prefer dataclasses over raw dicts for structured data models",
             "created_at": NOW},
        ]
        result = curate(memories, policy, now=NOW)
        result_ids = [m["id"] for m in result]

        assert "old" not in result_ids      # age eviction
        assert "a_dup" not in result_ids     # dedup
        assert "a" in result_ids             # first copy kept
        assert "b" in result_ids             # unique, kept

    def test_preserves_order(self):
        policy = MemoryPolicy(max_entries=100)
        memories = [
            {"id": "first", "content": "alpha lesson about module organization",
             "created_at": NOW},
            {"id": "second", "content": "beta lesson about dependency injection patterns",
             "created_at": NOW},
            {"id": "third", "content": "gamma lesson about error boundary design",
             "created_at": NOW},
        ]
        result = curate(memories, policy, now=NOW)
        assert [m["id"] for m in result] == ["first", "second", "third"]

    def test_empty_input(self):
        assert curate([], MemoryPolicy()) == []

    def test_default_policy_used(self):
        memories = [
            {"id": "m1", "content": "a useful lesson about testing strategies in production",
             "created_at": NOW},
        ]
        result = curate(memories, now=NOW)
        assert len(result) == 1

    def test_capacity_plus_dedup(self):
        policy = MemoryPolicy(max_entries=3, dedup_threshold=0.85)
        memories = [
            {"id": "a", "content": "Always handle errors explicitly in Go functions",
             "score": 0.9, "created_at": NOW},
            {"id": "b", "content": "Use structured logging with slog for observability",
             "score": 0.8, "created_at": NOW},
            {"id": "c", "content": "Prefer composition over inheritance in class design",
             "score": 0.7, "created_at": NOW},
            {"id": "d", "content": "Always handle errors explicitly in Go function code",
             "score": 0.6, "created_at": NOW},
            {"id": "e", "content": "Write table driven tests for comprehensive coverage",
             "score": 0.5, "created_at": NOW},
        ]
        result = curate(memories, policy, now=NOW)
        result_ids = [m["id"] for m in result]

        # d is a near-dup of a and should be deduped
        assert "d" not in result_ids
        # a, b, c have the highest scores and are distinct
        assert "a" in result_ids
