"""SPEC-5 WP4: Memory and learning contract tests."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.memory.rules import (
    GOOD_THRESHOLD,
    SIGNAL_REWARDS,
    VALID_SIGNALS,
    DEFAULT_RECALL_MIN_SIM,
    mmr_select,
    passes_similarity,
    reward_is_good,
    reward_score,
)
from sonder_runtime.application.memory.recall_service import (
    RecallService,
    _cosine_similarity,
)
from sonder_runtime.application.memory.outcome_service import (
    OutcomeService,
)
from sonder_runtime.domain.common.errors import InvalidInput


# ---------------------------------------------------------------------------
# Domain rules: reward validation
# ---------------------------------------------------------------------------

class TestRewardValidation:
    def test_signal_rewards_historical_values(self):
        """Historical reward values must never change (training data integrity)."""
        assert SIGNAL_REWARDS["tests_passed"] == 1.0
        assert SIGNAL_REWARDS["used"] == 0.9
        assert SIGNAL_REWARDS["copied"] == 0.85
        assert SIGNAL_REWARDS["edited"] == 0.75
        assert SIGNAL_REWARDS["accepted"] == 0.8
        assert SIGNAL_REWARDS["compiled"] == 0.7
        assert SIGNAL_REWARDS["rejected"] == -0.5
        assert SIGNAL_REWARDS["failed"] == -1.0

    def test_good_threshold_unchanged(self):
        assert GOOD_THRESHOLD == 0.71

    def test_compiled_below_good_threshold(self):
        assert not reward_is_good("compiled")

    def test_tests_passed_above_good_threshold(self):
        assert reward_is_good("tests_passed")

    def test_used_above_good_threshold(self):
        assert reward_is_good("used")

    def test_failed_not_good(self):
        assert not reward_is_good("failed")

    def test_unknown_signal_zero_reward(self):
        assert reward_score("nonexistent") == 0.0

    def test_valid_signals_frozenset(self):
        assert isinstance(VALID_SIGNALS, frozenset)
        assert len(VALID_SIGNALS) == 8


# ---------------------------------------------------------------------------
# Domain rules: similarity floor
# ---------------------------------------------------------------------------

class TestSimilarityFloor:
    def test_default_recall_min_sim_unchanged(self):
        assert DEFAULT_RECALL_MIN_SIM == 0.72

    def test_passes_at_threshold(self):
        assert passes_similarity(0.72, 0.72)

    def test_fails_below_threshold(self):
        assert not passes_similarity(0.71, 0.72)

    def test_passes_above_threshold(self):
        assert passes_similarity(0.99, 0.72)


# ---------------------------------------------------------------------------
# Domain rules: MMR deterministic
# ---------------------------------------------------------------------------

class TestMMRDeterministic:
    def _sim(self, a, b):
        return _cosine_similarity(a, b)

    def test_mmr_same_input_same_output(self):
        query = [1.0, 0.0, 0.0]
        candidates = [
            ("a", [1.0, 0.1, 0.0]),
            ("b", [0.9, 0.5, 0.0]),
            ("c", [0.1, 1.0, 0.0]),
        ]
        r1 = mmr_select(query, candidates, k=2, sim_fn=self._sim)
        r2 = mmr_select(query, candidates, k=2, sim_fn=self._sim)
        assert r1 == r2

    def test_mmr_diversifies(self):
        query = [1.0, 0.0]
        candidates = [
            ("near_clone_1", [1.0, 0.001]),
            ("near_clone_2", [1.0, 0.002]),
            ("diverse", [0.7, 0.71]),
        ]
        result = mmr_select(query, candidates, k=2, sim_fn=self._sim, lambda_mult=0.3)
        assert "near_clone_1" in result or "near_clone_2" in result
        assert "diverse" in result

    def test_mmr_empty_candidates(self):
        assert mmr_select([1.0], [], k=5, sim_fn=self._sim) == []

    def test_mmr_k_zero(self):
        assert mmr_select([1.0], [("a", [1.0])], k=0, sim_fn=self._sim) == []

    def test_mmr_null_query(self):
        result = mmr_select([], [("a", [1.0]), ("b", [0.5])], k=2, sim_fn=self._sim)
        assert result == ["a", "b"]


# ---------------------------------------------------------------------------
# Application: RecallService
# ---------------------------------------------------------------------------

class _FakeRecallStore:
    def __init__(self, lexical=None, semantic=None):
        self._lexical = lexical or []
        self._semantic = semantic or []

    def lexical_search(self, query, *, k, project, exclude_session):
        return self._lexical[:k]

    def semantic_candidates(self, *, k, project, exclude_session,
                            embedding_model, embedding_revision):
        return self._semantic[:k]


class _FakeEmbed:
    def embed(self, text):
        return [1.0, 0.0, 0.0]


class TestRecallService:
    def test_lexical_only_when_no_embedder(self):
        store = _FakeRecallStore(
            lexical=[{"text": "lesson A"}, {"text": "lesson B"}],
        )
        svc = RecallService(store, embed=None)
        result = svc.recall("test query", k=2)
        assert result == ["lesson A", "lesson B"]

    def test_semantic_recall_with_mmr(self):
        store = _FakeRecallStore(
            lexical=[],
            semantic=[
                {"id": "s1", "text": "semantic hit 1", "embedding": [1.0, 0.1, 0.0]},
                {"id": "s2", "text": "semantic hit 2", "embedding": [0.5, 0.87, 0.0]},
            ],
        )
        svc = RecallService(store, embed=_FakeEmbed())
        result = svc.recall("test", k=2, min_sim=0.0)
        assert len(result) <= 2
        assert all(isinstance(r, str) for r in result)

    def test_falls_back_to_lexical_on_no_semantic(self):
        store = _FakeRecallStore(
            lexical=[{"text": "fallback"}],
            semantic=[],
        )
        svc = RecallService(store, embed=_FakeEmbed())
        result = svc.recall("test", k=1, min_sim=0.0)
        assert result == ["fallback"]


# ---------------------------------------------------------------------------
# Application: OutcomeService
# ---------------------------------------------------------------------------

class _FakeOutcomeStore:
    def __init__(self, interaction=None):
        self._interaction = interaction
        self.outcomes = []
        self.events = []

    def get_interaction(self, iid):
        return self._interaction

    def record_outcome(self, iid, signal, reward, *, source):
        # Mirrors the OutcomeStore port exactly, including #62's required
        # provenance. A double that accepts less than the port does is a double
        # that has stopped proving the port is honoured.
        self.outcomes.append(
            {"id": iid, "signal": signal, "reward": reward, "source": source}
        )

    def append_outbox_event(self, event):
        self.events.append(event)


class TestOutcomeService:
    def test_record_valid_outcome(self):
        store = _FakeOutcomeStore(interaction={"id": "i1", "task": "test"})
        svc = OutcomeService(store)
        reward = svc.record("i1", "tests_passed")
        assert reward == 1.0
        assert len(store.outcomes) == 1
        assert store.outcomes[0]["signal"] == "tests_passed"
        # This service sits behind the caller-facing tool, so an unstated
        # provenance is `caller` -- and it must reach the store, not stop here.
        assert store.outcomes[0]["source"] == "caller"

    def test_outcome_emits_event(self):
        store = _FakeOutcomeStore(interaction={"id": "i1", "task": "test"})
        svc = OutcomeService(store)
        svc.record("i1", "used")
        assert len(store.events) == 1
        event = store.events[0]
        assert event.event_type == "outcome.recorded"
        assert event.aggregate_id == "i1"
        assert event.payload["signal"] == "used"
        assert event.payload["is_good"] is True

    def test_outcome_atomic_persist_and_event(self):
        """Outcome and outbox event are recorded in the same call sequence."""
        store = _FakeOutcomeStore(interaction={"id": "i1", "task": "test"})
        svc = OutcomeService(store)
        svc.record("i1", "compiled")
        assert len(store.outcomes) == 1
        assert len(store.events) == 1
        assert store.events[0].payload["is_good"] is False

    def test_invalid_signal_rejected(self):
        store = _FakeOutcomeStore(interaction={"id": "i1", "task": "test"})
        svc = OutcomeService(store)
        with pytest.raises(InvalidInput, match="unknown signal"):
            svc.record("i1", "bogus_signal")

    def test_missing_interaction_rejected(self):
        store = _FakeOutcomeStore(interaction=None)
        svc = OutcomeService(store)
        with pytest.raises(InvalidInput, match="not found"):
            svc.record("missing", "used")


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------

class TestCosineSimilarity:
    def test_identical_vectors(self):
        assert _cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite_vectors(self):
        assert _cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_zero_vector(self):
        assert _cosine_similarity([0, 0], [1, 1]) == pytest.approx(0.0)
