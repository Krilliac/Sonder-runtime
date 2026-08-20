from dataclasses import replace

import pytest

from sonder_runtime.application.skill_refresh import SkillRevision, SkillTrust
from sonder_runtime.application.skills.procedural_publication import (
    DurableLastGoodCatalog,
    HeldOutEvidence,
    InMemoryActiveSkillPort,
    PublicationError,
    PublicationState,
    ProceduralPublicationService,
    SkillPublication,
)
from sonder_runtime.application.promotion.gates import MeasuredPromotionGates
from sonder_runtime.domain.memory.wp6_typed import Evidence, EvidenceKind, MemoryLabel, TypedMemory
from sonder_runtime.domain.promotion.measured import MeasuredEvidence, PromotionArea, PromotionPolicy


def evidence(candidate_digest: str, *, passed: bool = True) -> HeldOutEvidence:
    return HeldOutEvidence("heldout-v1", "suite-sha", candidate_digest, "base-sha", passed, {"success": 1.0})


def candidate(skill_id: str, version: str, digest: str, ev: HeldOutEvidence) -> SkillPublication:
    return SkillPublication(skill_id, version, digest, "Use the bounded workflow.", ev.digest, "memory-1")


def revision(skill_id: str, version: str, digest: str) -> SkillRevision:
    return SkillRevision(skill_id, digest, version, SkillTrust.PROJECT)


def typed_memory() -> TypedMemory:
    return TypedMemory(
        "memory-1", "Use the bounded workflow.", MemoryLabel.PROCEDURAL,
        (Evidence(EvidenceKind.TEST_PASS, "suite", weight=1.0),), (),
    )


def approved(evidence_digest: str, rollback: str = "base-sha"):
    evidence = MeasuredEvidence(
        PromotionArea.SKILLS, "bounded@1", "base@sha", {"quality": 1.0}, True,
        ("eval-suite@sha",), rollback,
    )
    # The service compares the measured decision digest to HeldOutEvidence, so
    # callers must use the exact same digest-bearing evidence record.
    decision = MeasuredPromotionGates({PromotionArea.SKILLS: PromotionPolicy(
        minimums={"quality": 0.9}, required_provenance=("eval-suite@sha",),
    )}).evaluate(evidence)
    return decision


def test_publish_requires_matching_held_out_evidence_and_trusted_revision():
    ev = evidence("digest-1")
    catalog = DurableLastGoodCatalog()
    published = catalog.publish(candidate("bounded", "1", "digest-1", ev), revision("bounded", "1", "digest-1"), ev)
    assert published.state is PublicationState.ACTIVE
    assert catalog.current("bounded") == published

    with pytest.raises(PublicationError, match="different candidate"):
        catalog.publish(candidate("bounded", "2", "digest-2", ev), revision("bounded", "2", "digest-2"), ev)
    with pytest.raises(PublicationError, match="did not pass"):
        failed = evidence("digest-3", passed=False)
        catalog.publish(candidate("bounded", "3", "digest-3", failed), revision("bounded", "3", "digest-3"), failed)


def test_versioned_publish_retains_last_good_and_rolls_back():
    catalog = DurableLastGoodCatalog()
    first_evidence = evidence("digest-1")
    catalog.publish(candidate("bounded", "1", "digest-1", first_evidence), revision("bounded", "1", "digest-1"), first_evidence)
    second_evidence = evidence("digest-2")
    catalog.publish(candidate("bounded", "2", "digest-2", second_evidence), revision("bounded", "2", "digest-2"), second_evidence)
    assert catalog.last_good("bounded").version == "1"
    assert catalog.rollback("bounded").version == "1"
    assert catalog.current("bounded").version == "1"


def test_disablement_is_explicit_and_survives_snapshot_restore():
    catalog = DurableLastGoodCatalog()
    ev = evidence("digest-1")
    catalog.publish(candidate("bounded", "1", "digest-1", ev), revision("bounded", "1", "digest-1"), ev)
    catalog.disable("bounded", "operator quarantine")
    assert catalog.current("bounded") is None
    assert catalog.disabled_reason("bounded") == "operator quarantine"
    restored = DurableLastGoodCatalog.from_snapshot(catalog.snapshot())
    assert restored.disabled_reason("bounded") == "operator quarantine"
    with pytest.raises(PublicationError, match="disabled"):
        restored.publish(candidate("bounded", "2", "digest-2", evidence("digest-2")), revision("bounded", "2", "digest-2"), evidence("digest-2"))


def test_untrusted_and_tampered_snapshot_are_rejected():
    catalog = DurableLastGoodCatalog()
    ev = evidence("digest-1")
    with pytest.raises(PublicationError, match="trusted"):
        catalog.publish(candidate("bounded", "1", "digest-1", ev), revision("bounded", "1", "digest-1").__class__("bounded", "digest-1", "1", SkillTrust.UNTRUSTED), ev)
    catalog.publish(candidate("bounded", "1", "digest-1", ev), revision("bounded", "1", "digest-1"), ev)
    snapshot = replace(catalog.snapshot(), snapshot_digest="0" * 64)
    with pytest.raises(PublicationError, match="integrity"):
        DurableLastGoodCatalog.from_snapshot(snapshot)


def test_transactional_service_links_memory_provenance_and_active_skill():
    ev = evidence("digest-1")
    candidate_record = candidate("bounded", "1", "digest-1", ev)
    measured = MeasuredEvidence(
        PromotionArea.SKILLS, "bounded@1", "base@sha", {"quality": 1.0}, True,
        ("eval-suite@sha",), "base-sha",
    )
    decision = MeasuredPromotionGates({PromotionArea.SKILLS: PromotionPolicy(
        minimums={"quality": 0.9}, required_provenance=("eval-suite@sha",),
    )}).evaluate(measured)
    # Use the measured digest as the publication evidence digest while keeping
    # the held-out record as the candidate binding.
    candidate_record = replace(candidate_record, evidence_digest=ev.digest)
    service = ProceduralPublicationService(DurableLastGoodCatalog(), InMemoryActiveSkillPort())
    approved_decision = replace(decision, evidence_digest=ev.digest)
    published = service.publish(
        typed_memory(), candidate_record, revision("bounded", "1", "digest-1"), ev,
        approved_decision,
        source_interaction_ids=("interaction-1", "interaction-2"),
    )
    assert published.state is PublicationState.ACTIVE
    assert published.source_interaction_ids == ("interaction-1", "interaction-2")
    assert published.rollback_reference == "base-sha"
    assert published.promotion_evidence_digest == approved_decision.evidence_digest
    assert service.active.current("bounded") == published


def test_activation_failure_restores_catalog_and_active_state():
    class FailingActive(InMemoryActiveSkillPort):
        def activate(self, publication):
            raise RuntimeError("activation unavailable")

    ev = evidence("digest-1")
    measured = MeasuredEvidence(
        PromotionArea.SKILLS, "bounded@1", "base@sha", {"quality": 1.0}, True,
        ("eval-suite@sha",), "base-sha",
    )
    decision = MeasuredPromotionGates({PromotionArea.SKILLS: PromotionPolicy(
        minimums={"quality": 0.9}, required_provenance=("eval-suite@sha",),
    )}).evaluate(measured)
    catalog = DurableLastGoodCatalog()
    service = ProceduralPublicationService(catalog, FailingActive())
    with pytest.raises(PublicationError, match="rolled back"):
        service.publish(
            typed_memory(), candidate("bounded", "1", "digest-1", ev),
            revision("bounded", "1", "digest-1"), ev, replace(decision, evidence_digest=ev.digest),
            source_interaction_ids=("interaction-1",),
        )
    assert catalog.current("bounded") is None


def test_service_rollback_restores_active_skill_and_last_good_version():
    ev1 = evidence("digest-1")
    ev2 = evidence("digest-2")
    measured = MeasuredEvidence(
        PromotionArea.SKILLS, "bounded@1", "base-sha", {"quality": 1.0}, True,
        ("eval-suite@sha",), "base-sha",
    )
    decision = MeasuredPromotionGates({PromotionArea.SKILLS: PromotionPolicy(
        minimums={"quality": 0.9}, required_provenance=("eval-suite@sha",),
    )}).evaluate(measured)
    catalog = DurableLastGoodCatalog()
    active = InMemoryActiveSkillPort()
    service = ProceduralPublicationService(catalog, active)
    service.publish(typed_memory(), candidate("bounded", "1", "digest-1", ev1), revision("bounded", "1", "digest-1"), ev1, replace(decision, evidence_digest=ev1.digest), source_interaction_ids=("i1",))
    service.publish(typed_memory(), candidate("bounded", "2", "digest-2", ev2), revision("bounded", "2", "digest-2"), ev2, replace(decision, candidate="bounded@2", evidence_digest=ev2.digest), source_interaction_ids=("i2",))
    restored = service.rollback("bounded")
    assert restored.version == "1"
    assert active.current("bounded").version == "1"
