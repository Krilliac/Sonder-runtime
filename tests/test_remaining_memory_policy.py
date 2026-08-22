from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.application.memory.memory_policy import (
    EmbeddingBinding,
    EmbeddingIdentity,
    MemoryClass,
    MemoryPolicy,
    PrivacyClass,
    PromotionDisposition,
    RetrievalDisposition,
    TemporalTruth,
    evaluate_retrieval,
    evaluate_write,
    link_procedural_promotion,
    memory_class_for_label,
    policy_for,
)
from sonder_runtime.domain.memory.wp6_typed import (
    Contradiction,
    Evidence,
    EvidenceKind,
    MemoryLabel,
    TypedMemory,
)


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def test_policy_matrix_covers_all_required_classes_and_separates_working_memory():
    assert {item.value for item in MemoryClass} == {
        "working", "episodic", "semantic", "procedural", "preference",
        "project", "failure", "artifact",
    }
    assert policy_for(MemoryClass.WORKING).promotion_enabled is False
    assert policy_for(MemoryClass.PROCEDURAL).promotion_enabled is True
    assert policy_for(MemoryClass.PREFERENCE).private_by_default is True
    assert memory_class_for_label(MemoryLabel.FACTUAL) is MemoryClass.SEMANTIC
    assert MemoryPolicy().for_class("procedural").promotion_enabled


def test_write_policy_requires_evidence_provenance_and_rejects_secrets():
    denied = evaluate_write(
        MemoryClass.SEMANTIC, confidence=0.9, privacy=PrivacyClass.PUBLIC,
    )
    assert not denied.allowed
    assert {"provenance is required", "evidence or explicit confirmation is required"} <= set(denied.reasons)

    secret = evaluate_write(
        MemoryClass.WORKING, confidence=0.0, privacy=PrivacyClass.SECRET,
        content="token",
    )
    assert not secret.allowed
    assert "secret content is not memory-store eligible" in secret.reasons


def test_temporal_truth_supports_boundaries_decay_revalidation_and_relations():
    truth = TemporalTruth(
        valid_from=NOW - timedelta(days=10), valid_until=NOW + timedelta(days=10),
        supersedes=("old",), contradicts=("counterexample",),
        source_trust=0.8, confidence=0.9,
    )
    truth.validate_relationships("current")
    assert truth.is_valid_at(NOW)
    assert not truth.is_valid_at(NOW + timedelta(days=10))
    assert 0.0 < truth.decay(NOW + timedelta(days=180)) < 1.0
    refreshed = truth.revalidate(NOW, confidence=1.0)
    assert refreshed.last_revalidated_at == NOW
    assert refreshed.confidence == 1.0

    with pytest.raises(ValueError, match="both superseded"):
        TemporalTruth(NOW, supersedes=("same",), contradicts=("same",))


def test_retrieval_returns_score_provenance_freshness_and_exclusion_reason():
    allowed = evaluate_retrieval(
        "m1", MemoryClass.PROCEDURAL, score_components={"semantic": 0.9, "mmr": 0.8},
        provenance=("test-run-1",), confidence=0.9, freshness=0.7,
        requested_scope="project-a",
    )
    assert allowed.disposition is RetrievalDisposition.ALLOW
    assert allowed.score_components["semantic"] == 0.9
    assert allowed.provenance == ("test-run-1",)

    denied = evaluate_retrieval(
        "m2", MemoryClass.SEMANTIC, confidence=0.8, freshness=0.8,
    )
    assert denied.disposition is RetrievalDisposition.EXCLUDE
    assert "retrieval scope required" in denied.exclusion_reasons


def test_embedding_identity_binds_all_space_metadata_and_vector_digest():
    identity = EmbeddingIdentity(
        model="embedder", revision="r7", dimensions=2, normalization="none",
        truncation="none", serving_implementation="cpu-v1",
    )
    binding = EmbeddingBinding.from_vector(identity, [0.25, 0.75])
    assert len(binding.vector_digest) == 64
    assert identity.matches(EmbeddingIdentity(*identity.key))
    assert not identity.matches(EmbeddingIdentity(
        "embedder", "r8", 2, "none", "none", "cpu-v1",
    ))
    with pytest.raises(ValueError, match="dimension"):
        EmbeddingBinding.from_vector(identity, [1.0])


def test_l2_embedding_identity_rejects_non_unit_vectors():
    identity = EmbeddingIdentity("e", "1", 2, "l2", "none", "runtime")
    with pytest.raises(ValueError, match="unit"):
        EmbeddingBinding.from_vector(identity, [1.0, 1.0])
    assert EmbeddingBinding.from_vector(identity, [1.0, 0.0]).identity == identity


def test_procedural_promotion_links_wp6_memory_to_baseline_and_rollback():
    memory = TypedMemory(
        "mem-1", "Use the verified migration command.", MemoryLabel.PROCEDURAL,
        evidence=(Evidence(EvidenceKind.TEST_PASS, "ci", NOW, 1.0, "run-1"),),
    )
    link = link_procedural_promotion(
        memory, candidate_id="skill-1", source_interaction_ids=("run-1", "run-1"),
        baseline_digest="base", candidate_digest="candidate", rollback_reference="route-0",
    )
    assert link.source_interaction_ids == ("run-1",)
    assert link.disposition is PromotionDisposition.CANDIDATE

    with pytest.raises(ValueError, match="contradictory"):
        link_procedural_promotion(
            TypedMemory(
                "mem-2", "unsafe", MemoryLabel.PROCEDURAL,
                evidence=(Evidence(EvidenceKind.EXPLICIT, "user"),),
                contradictions=(Contradiction("test", "failed"),),
            ),
            candidate_id="skill-2", source_interaction_ids=("run-2",),
            baseline_digest="base", candidate_digest="candidate", rollback_reference="route-0",
        )
