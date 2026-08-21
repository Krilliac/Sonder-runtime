from dataclasses import replace

import pytest

from sonder_runtime.application.skill_refresh import SkillRevision, SkillTrust
from sonder_runtime.application.skills.composition import (
    build_procedural_publication_composition,
)
from sonder_runtime.application.skills.procedural_publication import (
    HeldOutEvidence,
    InMemoryActiveSkillPort,
    PublicationError,
    PublicationState,
    SkillPublication,
)
from sonder_runtime.application.promotion.gates import MeasuredPromotionGates
from sonder_runtime.domain.memory.wp6_typed import (
    Evidence,
    EvidenceKind,
    MemoryLabel,
    TypedMemory,
)
from sonder_runtime.domain.promotion.measured import (
    MeasuredEvidence,
    PromotionArea,
    PromotionPolicy,
)


def _memory() -> TypedMemory:
    return TypedMemory(
        "memory-1", "Use the bounded workflow.", MemoryLabel.PROCEDURAL,
        (Evidence(EvidenceKind.TEST_PASS, "heldout-suite", weight=1.0),), (),
    )


def _evidence(digest: str) -> HeldOutEvidence:
    return HeldOutEvidence("heldout-v1", "suite-sha", digest, "base-sha", True, {"success": 1.0})


def _decision(candidate: str = "bounded@1"):
    measured = MeasuredEvidence(
        PromotionArea.SKILLS, candidate, "base@sha", {"quality": 1.0}, True,
        ("eval-suite@sha",), "base-sha",
    )
    return MeasuredPromotionGates({PromotionArea.SKILLS: PromotionPolicy(
        minimums={"quality": 0.9}, required_provenance=("eval-suite@sha",),
    )}).evaluate(measured)


def _candidate(ev: HeldOutEvidence, version: str = "1") -> SkillPublication:
    return SkillPublication("bounded", version, f"digest-{version}", "Use the bounded workflow.", ev.digest, "memory-1")


def test_typed_composition_connects_memory_provenance_and_holdout_to_publication():
    ev = _evidence("digest-1")
    active = InMemoryActiveSkillPort()
    graph = build_procedural_publication_composition(active=active)

    published = graph.publish(
        _memory(), _candidate(ev), SkillRevision("bounded", "digest-1", "1", SkillTrust.PROJECT),
        ev, replace(_decision(), evidence_digest=ev.digest),
        source_interaction_ids=("interaction-1",),
    )

    assert published.state is PublicationState.ACTIVE
    assert published.candidate_memory_id == "memory-1"
    assert published.source_interaction_ids == ("interaction-1",)
    assert published.evidence_digest == ev.digest
    assert active.current("bounded") == published


def test_composition_fails_closed_before_mutating_catalog_without_provenance():
    ev = _evidence("digest-1")
    graph = build_procedural_publication_composition(active=InMemoryActiveSkillPort())

    with pytest.raises(ValueError, match="provenance"):
        graph.publish(
            _memory(), _candidate(ev), SkillRevision("bounded", "digest-1", "1", SkillTrust.PROJECT),
            ev, replace(_decision(), evidence_digest=ev.digest), source_interaction_ids=(),
        )

    assert graph.catalog.current("bounded") is None


def test_composition_rollback_restores_last_good_and_preserves_active_port():
    active = InMemoryActiveSkillPort()
    graph = build_procedural_publication_composition(active=active)
    first = _evidence("digest-1")
    second = _evidence("digest-2")
    graph.publish(
        _memory(), _candidate(first), SkillRevision("bounded", "digest-1", "1", SkillTrust.PROJECT),
        first, replace(_decision(), evidence_digest=first.digest), source_interaction_ids=("i1",),
    )
    graph.publish(
        _memory(), _candidate(second, "2"), SkillRevision("bounded", "digest-2", "2", SkillTrust.PROJECT),
        second, replace(_decision("bounded@2"), evidence_digest=second.digest), source_interaction_ids=("i2",),
    )

    restored = graph.rollback("bounded")
    assert restored.version == "1"
    assert active.current("bounded") == restored


def test_composition_rejects_non_procedural_memory_without_mutation():
    ev = _evidence("digest-1")
    graph = build_procedural_publication_composition(active=InMemoryActiveSkillPort())
    factual = replace(_memory(), label=MemoryLabel.FACTUAL)

    with pytest.raises(ValueError, match="procedural"):
        graph.publish(
            factual, _candidate(ev), SkillRevision("bounded", "digest-1", "1", SkillTrust.PROJECT),
            ev, replace(_decision(), evidence_digest=ev.digest), source_interaction_ids=("i1",),
        )
    assert graph.catalog.current("bounded") is None
