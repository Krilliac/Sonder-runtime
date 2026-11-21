"""Injected ``MemoryPolicy`` mappings must govern enforcement, not only inspection."""
from __future__ import annotations

import copy
import pickle
from dataclasses import replace

import pytest

from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.application.memory.facade import MemoryLearningFacade
from sonder_runtime.application.memory.memory_policy import (
    MEMORY_CLASS_POLICIES,
    MemoryClass,
    MemoryPolicy,
    evaluate_retrieval,
    evaluate_write,
)
from sonder_runtime.application.promotion.gates import MeasuredPromotionGates
from sonder_runtime.application.skill_refresh import SkillRevision, SkillTrust
from sonder_runtime.application.skills.composition import (
    build_procedural_publication_composition,
)
from sonder_runtime.application.skills.procedural_publication import (
    HeldOutEvidence,
    InMemoryActiveSkillPort,
    PublicationState,
    SkillPublication,
)
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

SEMANTIC = MemoryClass.SEMANTIC
WRITE = {"confidence": 0.9, "provenance": ("i1",), "evidence_count": 1}
RETRIEVE = {"confidence": 0.9, "requested_scope": "project-a", "provenance": ("i1",)}


def _matrix(**semantic_changes) -> dict:
    """A complete mapping whose semantic entry differs from the default."""
    matrix = dict(MEMORY_CLASS_POLICIES)
    matrix[SEMANTIC] = replace(MEMORY_CLASS_POLICIES[SEMANTIC], **semantic_changes)
    return matrix


def _strict() -> MemoryPolicy:
    return MemoryPolicy(policies=_matrix(write_min_confidence=1.0, retrieval_enabled=False))


def test_injected_tighter_confidence_denies_write_with_explanation():
    decision = _strict().write("semantic", **WRITE)

    assert decision.allowed is False
    assert "confidence below class threshold" in decision.reasons
    # The same request is admitted by the default matrix, so the denial is
    # caused by the injected mapping and nothing else.
    assert MemoryPolicy().write("semantic", **WRITE).allowed is True


def test_injected_disabled_retrieval_excludes_with_explanation():
    decision = _strict().retrieve("m1", "semantic", **RETRIEVE)

    assert decision.included is False
    assert "retrieval disabled for class" in decision.exclusion_reasons
    assert MemoryPolicy().retrieve("m1", "semantic", **RETRIEVE).included is True


def test_inspection_write_and_retrieval_agree_on_one_injected_mapping():
    policy = _strict()
    inspected = policy.for_class("semantic")

    assert (inspected.write_min_confidence, inspected.retrieval_enabled) == (1.0, False)
    assert policy.write(SEMANTIC, **WRITE).allowed is False
    assert policy.write(SEMANTIC, **{**WRITE, "confidence": 1.0}).allowed is True
    assert policy.retrieve("m1", SEMANTIC, **RETRIEVE).included is False
    # Classes the mapping left at their defaults keep default behavior.
    assert policy.write("episodic", **WRITE).allowed is True
    assert policy.retrieve("m1", "episodic", **RETRIEVE).included is True


def test_two_instances_with_different_policies_are_independent():
    before = dict(MEMORY_CLASS_POLICIES)
    strict = _strict()
    lenient = MemoryPolicy(policies=_matrix(write_min_confidence=0.1))
    low = {**WRITE, "confidence": 0.2}

    # Interleaved on purpose: neither evaluation may leak into the next.
    assert strict.write("semantic", **WRITE).allowed is False
    assert lenient.write("semantic", **low).allowed is True
    assert strict.write("semantic", **low).allowed is False
    assert strict.retrieve("m1", "semantic", **RETRIEVE).included is False
    assert lenient.retrieve("m1", "semantic", **RETRIEVE).included is True
    assert MemoryPolicy().write("semantic", **low).allowed is False

    assert dict(MEMORY_CLASS_POLICIES) == before
    assert MEMORY_CLASS_POLICIES[SEMANTIC].write_min_confidence == 0.70
    assert strict.revision != lenient.revision


def test_provenance_requirement_follows_the_injected_policy():
    no_provenance = {"confidence": 0.9, "evidence_count": 1}
    relaxed = MemoryPolicy(policies=_matrix(write_requires_provenance=False))

    assert MemoryPolicy().write("semantic", **no_provenance).allowed is False
    assert relaxed.write("semantic", **no_provenance).allowed is True

    working = replace(
        MEMORY_CLASS_POLICIES[MemoryClass.WORKING],
        write_requires_provenance=True,
        write_requires_explicit_or_evidence=True,
    )
    tightened = MemoryPolicy.with_overrides(working)
    assert MemoryPolicy().write("working", confidence=0.5).allowed is True
    denied = tightened.write("working", confidence=0.5)
    assert denied.allowed is False
    assert {
        "provenance is required",
        "evidence or explicit confirmation is required",
    } <= set(denied.reasons)


def test_scope_requirement_follows_the_injected_policy():
    unscoped = {"confidence": 0.9}
    relaxed = MemoryPolicy(policies=_matrix(retrieval_requires_scope=False))

    assert MemoryPolicy().retrieve("m1", "semantic", **unscoped).included is False
    assert relaxed.retrieve("m1", "semantic", **unscoped).included is True


def test_memory_learning_facade_enforces_its_injected_policy(tmp_path):
    def unit_of_work():
        return UnitOfWorkAdapter(str(tmp_path / "memory.db"))

    facade = MemoryLearningFacade(unit_of_work, policy=_strict())

    write = facade.evaluate_write("semantic", **WRITE)
    assert write.allowed is False
    assert "confidence below class threshold" in write.reasons
    retrieval = facade.evaluate_retrieval("m1", "semantic", **RETRIEVE)
    assert retrieval.included is False
    assert "retrieval disabled for class" in retrieval.exclusion_reasons
    # What a diagnostics reader sees is what was enforced.
    assert facade.policy.for_class("semantic").write_min_confidence == 1.0
    assert write.policy_revision == facade.policy.revision

    default = MemoryLearningFacade(unit_of_work)
    assert default.evaluate_write("semantic", **WRITE).allowed is True
    assert default.evaluate_retrieval("m1", "semantic", **RETRIEVE).included is True


def _procedural_memory() -> TypedMemory:
    return TypedMemory(
        "memory-1", "Use the bounded workflow.", MemoryLabel.PROCEDURAL,
        (Evidence(EvidenceKind.TEST_PASS, "heldout-suite", weight=0.9),), (),
    )


def _publish(graph):
    evidence = HeldOutEvidence(
        "heldout-v1", "suite-sha", "digest-1", "base-sha", True, {"success": 1.0},
    )
    measured = MeasuredEvidence(
        PromotionArea.SKILLS, "bounded@1", "base@sha", {"quality": 1.0}, True,
        ("eval-suite@sha",), "base-sha",
    )
    decision = MeasuredPromotionGates({PromotionArea.SKILLS: PromotionPolicy(
        minimums={"quality": 0.9}, required_provenance=("eval-suite@sha",),
    )}).evaluate(measured)
    return graph.publish(
        _procedural_memory(),
        SkillPublication(
            "bounded", "1", "digest-1", "Use the bounded workflow.",
            evidence.digest, "memory-1",
        ),
        SkillRevision("bounded", "digest-1", "1", SkillTrust.PROJECT),
        evidence,
        replace(decision, evidence_digest=evidence.digest),
        source_interaction_ids=("interaction-1",),
    )


class _RecordingEvents:
    def __init__(self) -> None:
        self.calls: list = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


def test_strict_procedural_policy_denies_publication_without_side_effects():
    # The fixture memory's support score sits between the default threshold
    # and the strict one, so only the injected policy can deny it.
    support = min(1.0, _procedural_memory().support_score)
    default_threshold = MEMORY_CLASS_POLICIES[MemoryClass.PROCEDURAL].write_min_confidence
    assert default_threshold <= support < 1.0

    strict = MemoryPolicy.with_overrides(
        replace(MEMORY_CLASS_POLICIES[MemoryClass.PROCEDURAL], write_min_confidence=1.0)
    )
    active = InMemoryActiveSkillPort()
    events = _RecordingEvents()
    graph = build_procedural_publication_composition(
        active=active, events=events, memory_policy=strict,
    )
    catalog_before = graph.catalog.snapshot()
    active_before = active.snapshot()

    with pytest.raises(ValueError, match="procedural memory admission denied") as raised:
        _publish(graph)

    assert "confidence below class threshold" in str(raised.value)
    assert graph.catalog.current("bounded") is None
    assert graph.catalog.snapshot() == catalog_before
    assert active.current("bounded") is None
    assert active.snapshot() == active_before
    assert events.calls == []

    # Control: the identical request publishes under the default policy.
    default_active = InMemoryActiveSkillPort()
    published = _publish(build_procedural_publication_composition(active=default_active))
    assert published.state is PublicationState.ACTIVE
    assert default_active.current("bounded") == published


def test_default_policy_matches_the_standalone_evaluators():
    default = MemoryPolicy()
    assert dict(default.policies) == dict(MEMORY_CLASS_POLICIES)
    assert MemoryPolicy.with_overrides().revision == default.revision

    write_cases = (
        {"confidence": 0.9, "provenance": ("i1",), "evidence_count": 1},
        {"confidence": 0.1, "provenance": ("i1",), "evidence_count": 1},
        {"confidence": 0.9},
        {"confidence": 0.9, "provenance": ("i1",), "explicit_confirmation": True},
        {"confidence": 0.9, "provenance": ("i1",), "evidence_count": 1, "privacy": "secret"},
    )
    retrieve_cases = (
        {"confidence": 0.9, "requested_scope": "project-a"},
        {"confidence": 0.9},
        {"confidence": 0.9, "requested_scope": "project-a", "freshness": 0.0},
        {"confidence": 0.9, "requested_scope": "project-a", "privacy": "private"},
    )
    for kind in MemoryClass:
        assert default.for_class(kind) is MEMORY_CLASS_POLICIES[kind]
        for case in write_cases:
            assert replace(default.write(kind, **case), policy_revision=None) == (
                evaluate_write(kind, **case)
            )
        for case in retrieve_cases:
            assert replace(default.retrieve("m1", kind, **case), policy_revision=None) == (
                evaluate_retrieval("m1", kind, **case)
            )


def test_standalone_evaluators_accept_an_explicit_class_policy():
    strict = replace(
        MEMORY_CLASS_POLICIES[SEMANTIC], write_min_confidence=1.0, retrieval_enabled=False,
    )

    assert evaluate_write(SEMANTIC, policy=strict, **WRITE).allowed is False
    assert evaluate_retrieval("m1", SEMANTIC, policy=strict, **RETRIEVE).included is False
    assert evaluate_write(SEMANTIC, **WRITE).policy_revision is None

    with pytest.raises(ValueError, match="cannot govern"):
        evaluate_write(MemoryClass.EPISODIC, policy=strict, **WRITE)
    with pytest.raises(ValueError, match="cannot govern"):
        evaluate_retrieval("m1", MemoryClass.EPISODIC, policy=strict, **RETRIEVE)


def test_facade_methods_cannot_be_redirected_to_another_policy():
    lenient = replace(MEMORY_CLASS_POLICIES[SEMANTIC], write_min_confidence=0.0)

    with pytest.raises(TypeError):
        _strict().write("semantic", policy=lenient, **WRITE)
    with pytest.raises(TypeError):
        _strict().retrieve("m1", "semantic", policy=lenient, **RETRIEVE)


def test_incomplete_or_mislabelled_mappings_are_rejected():
    with pytest.raises(ValueError, match="missing: .*working"):
        MemoryPolicy(policies={SEMANTIC: MEMORY_CLASS_POLICIES[SEMANTIC]})

    swapped = dict(MEMORY_CLASS_POLICIES)
    swapped[SEMANTIC] = MEMORY_CLASS_POLICIES[MemoryClass.WORKING]
    with pytest.raises(ValueError, match="registered under"):
        MemoryPolicy(policies=swapped)

    string_keys = {kind.value: item for kind, item in MEMORY_CLASS_POLICIES.items()}
    with pytest.raises(ValueError, match="must be a MemoryClass"):
        MemoryPolicy(policies=string_keys)

    wrong_value = dict(MEMORY_CLASS_POLICIES)
    wrong_value[SEMANTIC] = {"write_min_confidence": 0.0}
    with pytest.raises(ValueError, match="must be a MemoryClassPolicy"):
        MemoryPolicy(policies=wrong_value)

    with pytest.raises(ValueError, match="duplicate override"):
        MemoryPolicy.with_overrides(
            MEMORY_CLASS_POLICIES[SEMANTIC], MEMORY_CLASS_POLICIES[SEMANTIC],
        )


def test_policy_is_frozen_against_later_caller_mutation():
    matrix = _matrix(write_min_confidence=1.0)
    policy = MemoryPolicy(policies=matrix)
    revision = policy.revision

    matrix[SEMANTIC] = MEMORY_CLASS_POLICIES[SEMANTIC]
    del matrix[MemoryClass.WORKING]

    assert policy.for_class("semantic").write_min_confidence == 1.0
    assert policy.write("semantic", **WRITE).allowed is False
    assert policy.for_class("working") is MEMORY_CLASS_POLICIES[MemoryClass.WORKING]
    assert policy.revision == revision
    with pytest.raises(TypeError):
        policy.policies[SEMANTIC] = MEMORY_CLASS_POLICIES[SEMANTIC]  # type: ignore[index]


def test_revision_identifies_the_effective_matrix_and_is_stamped_on_decisions():
    strict = _strict()
    same = _strict()
    default = MemoryPolicy()

    assert strict.revision == same.revision
    assert strict == same
    assert strict.revision != default.revision
    assert len(strict.revision) == 64
    assert strict.write("semantic", **WRITE).policy_revision == strict.revision
    assert strict.retrieve("m1", "semantic", **RETRIEVE).policy_revision == strict.revision
    assert default.write("semantic", **WRITE).policy_revision == default.revision


# The pickle round trip below only loads bytes this test produced itself.
def test_policy_survives_copy_and_pickle_with_enforcement_intact():
    strict = _strict()

    for clone in (copy.deepcopy(strict), pickle.loads(pickle.dumps(strict))):
        assert clone == strict
        assert clone.revision == strict.revision
        assert clone.write("semantic", **WRITE).allowed is False
