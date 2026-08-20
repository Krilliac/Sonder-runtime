from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.application.memory.procedural_learning import (
    ProceduralLearningService,
    PromotionPolicy,
)
from sonder_runtime.domain.memory.wp6_typed import (
    Contradiction,
    Evidence,
    EvidenceKind,
    MemoryLabel,
    TypedMemory,
)


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def memory(name, *, evidence=(), contradictions=(), age=timedelta(days=1), label=MemoryLabel.PROCEDURAL):
    return TypedMemory(
        name, name, label, tuple(evidence), tuple(contradictions),
        created_at=NOW - age,
    )


def test_typed_memory_scores_evidence_and_contradictions():
    item = memory("Use a bounded queue.", evidence=(
        Evidence(EvidenceKind.TEST_PASS, "pytest", weight=1.0),
        Evidence(EvidenceKind.USED, "caller", weight=0.5),
    ), contradictions=(Contradiction("review", "overflow case", severity=.5),))

    assert item.is_procedural
    assert item.support_score == 1.5
    assert item.contradiction_score == .5


def test_promotion_requires_evidence_and_excludes_stale_or_contradictory_items():
    good = memory("Prefer pathlib.", evidence=(Evidence(EvidenceKind.TEST_PASS, "ci"),))
    stale = memory("Use the old helper.", evidence=(Evidence(EvidenceKind.USED, "caller"),), age=timedelta(days=90))
    contradicted = memory("Retry forever.", evidence=(Evidence(EvidenceKind.USED, "caller"),), contradictions=(Contradiction("review", "unsafe"),))
    untyped = memory("Remember this.", evidence=(Evidence(EvidenceKind.EXPLICIT, "user"),), label=MemoryLabel.EPISODIC)

    result = ProceduralLearningService(PromotionPolicy(max_age=timedelta(days=30))).candidates(
        [good, stale, contradicted, untyped], now=NOW,
    )
    assert [candidate.memory_id for candidate in result] == ["Prefer pathlib."]


def test_promotion_is_deduplicated_sorted_and_bounded():
    rows = [memory(str(index), evidence=(Evidence(EvidenceKind.USED, "caller", weight=index),)) for index in range(1, 5)]
    rows.append(memory(" 1 ", evidence=(Evidence(EvidenceKind.USED, "caller", weight=99),)))
    service = ProceduralLearningService(PromotionPolicy(max_candidates=2))

    result = service.candidates(rows, now=NOW)
    assert len(result) == 2
    assert [candidate.evidence_score for candidate in result] == [99, 4]


def test_invalid_evidence_and_bounds_are_rejected():
    with pytest.raises(ValueError):
        Evidence(EvidenceKind.USED, "caller", weight=-1)
    with pytest.raises(ValueError):
        PromotionPolicy(max_candidates=0)

