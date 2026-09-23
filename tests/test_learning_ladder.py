from datetime import datetime, timezone

from sonder_runtime.application.memory.learning_ladder import (
    LearningLadder, LearningObservation, LearningStage,
)


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


def obs(number, source, *, positive=True, trusted=True, confirm=False, evaluated=False, content="always use a bounded timeout"):
    return LearningObservation(
        f"obs-{number}", content, source, source,
        provenance=(f"trace-{number}",), confidence=0.9,
        observed_at=NOW, positive=positive,
        explicit_confirmation=confirm, trusted_source=trusted,
        evaluation_passed=evaluated,
    )


def test_independent_evidence_promotes_through_fact_heuristic_and_policy():
    ladder = LearningLadder()
    fact = ladder.evaluate([obs(1, "test-a"), obs(2, "test-b")])[0]
    assert fact.stage is LearningStage.FACT
    heuristic = ladder.evaluate([obs(i, f"test-{i}") for i in range(3)])[0]
    assert heuristic.stage is LearningStage.HEURISTIC
    policy = ladder.evaluate([obs(i, f"test-{i}", confirm=i == 0, evaluated=i == 0) for i in range(5)])[0]
    assert policy.stage is LearningStage.POLICY
    assert policy.promotable


def test_policy_promotion_requires_evaluation_as_well_as_confirmation():
    decision = LearningLadder().evaluate([
        obs(i, f"test-{i}", confirm=i == 0) for i in range(5)
    ])[0]
    assert decision.stage is LearningStage.HEURISTIC


def test_repeated_same_source_does_not_count_as_independent_evidence():
    decision = LearningLadder().evaluate([obs(1, "same"), obs(2, "same"), obs(3, "same")])[0]
    assert decision.stage is LearningStage.CANDIDATE
    assert decision.independent_sources == ("same",)


def test_untrusted_evidence_cannot_promote_and_contradiction_demotes():
    untrusted = LearningLadder().evaluate([obs(1, "a", trusted=False), obs(2, "b", trusted=False)])[0]
    assert untrusted.stage is LearningStage.CANDIDATE
    contradiction = LearningLadder().evaluate([obs(1, "a"), obs(2, "b"), obs(3, "counter", positive=False)])[0]
    assert contradiction.stage is LearningStage.CANDIDATE
    assert contradiction.contradiction_count == 1
    assert "demotes" in contradiction.reason


def test_empty_and_invalid_observations_fail_closed():
    assert LearningLadder().evaluate([]) == ()
    try:
        obs(1, "source", content="")
    except ValueError:
        pass
    else:
        raise AssertionError("empty observations must be rejected")
