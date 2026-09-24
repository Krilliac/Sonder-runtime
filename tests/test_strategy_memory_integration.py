"""Sealed strategy outcomes feed scoped, evidence-ranked memory references."""

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import (
    SQLiteRuntimeCheckpointRepository,
)
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.application.context_planner import (
    CONTEXT_SECTIONS,
    ContextPlanner,
    ModelContext,
)
from sonder_runtime.application.memory.learning_ladder import LearningStage
from sonder_runtime.application.memory.receipt_observation import (
    ReceiptObservationProducer,
)
from sonder_runtime.application.memory.strategy_memory import (
    StrategyMemoryService,
    language_from_path,
)
from sonder_runtime.application.ports.host_final import HostFinalFacts
from sonder_runtime.application.ports.host_turn_links import (
    FinalizedHostResult,
    ManagedHostFinalEvidence,
    ManagedHostTerminalLink,
    ManagedHostTurnLink,
)
from sonder_runtime.application.ports.lane_continuation import (
    PendingVerificationIdentity,
)
from sonder_runtime.application.ports.terminal_eligibility import (
    ManagedTerminalEligibility,
    _issue_host_verifier_authority,
)
from sonder_runtime.application.strategy.tracing import StrategyTraceService
from sonder_runtime.domain.strategy.models import (
    EvidenceRef,
    FailureClass,
    FailureObservation,
    StrategyAction,
    StrategyAttempt,
    StrategyBudget,
    StrategySignature,
    StrategyUsage,
)


def _application(tmp_path):
    checkpoint = SQLiteRuntimeCheckpointRepository(tmp_path / "runtime.sqlite", seal_key=b"k" * 32)
    trace = StrategyTraceService(checkpoint)
    memory_path = tmp_path / "memory.sqlite"
    return trace, StrategyMemoryService(trace, lambda: UnitOfWorkAdapter(str(memory_path))), memory_path


def _attempt(run_id, *, outcome="succeeded", evidence=()):
    return StrategyAttempt(
        run_id, "attempt-1",
        StrategySignature("patch", "a" * 64, ("src/module.py",), "b" * 64,
                          "private rationale: password=never-index-this", "pytest -q"),
        outcome,
        failure=FailureObservation(FailureClass.TEST_FAILURE, evidence_digest="f" * 64)
        if outcome == "failed" else None,
        usage=StrategyUsage(attempts=1, model_calls=1, verifier_calls=1),
        evidence=tuple(evidence),
    )


def _record(trace, attempt):
    return trace.record(attempt, budget=StrategyBudget(), available_actions=(StrategyAction.REPAIR,))


def _plan(tokens=96):
    budgets = dict.fromkeys(CONTEXT_SECTIONS, 0)
    budgets["memories"] = tokens
    return ContextPlanner().plan(ModelContext("model", max(256, tokens + 64), 64),
                                 {"memories": tokens}, budgets)


def _verified(run_id, *, principal, project="project-a"):
    receipt_id = hashlib.sha256(f"{run_id}:{principal}".encode()).hexdigest()
    turn = ManagedHostTurnLink("continuation-1", "parent-1", "conversation-1", principal, run_id, 1)
    link = ManagedHostTerminalLink(
        turn, "original-id", "a" * 64, "final-id", "b" * 64,
        receipt_id, hashlib.sha256(b"verification output").hexdigest(),
    )
    facts = HostFinalFacts(
        (), project, True, True, True, "NORMAL", certificate_id="cert-" + run_id,
        certificate_generation=1, certificate_code="verified-change", delegated_work=True,
    )
    evidence = ManagedHostFinalEvidence(FinalizedHostResult("verification output", link), facts)
    identity = PendingVerificationIdentity(
        "continuation-1", "verification-1", "parent-1", 1, 1,
        "b" * 64, "command-1", "a" * 64, 1,
    )
    outcome = ManagedTerminalEligibility(
        evidence, True, "certified", "CERTIFIED", pending_identity=identity,
        authenticated_worker_id="lane-" + run_id, verified_subject_digest="a" * 64,
    )
    holder = {}
    authority = _issue_host_verifier_authority(lambda: holder["value"])
    holder["value"] = replace(outcome, authority=authority)
    return ReceiptObservationProducer.from_terminal_eligibility(holder["value"])


def test_strategy_outcomes_are_content_free_and_scoped_after_restart(tmp_path):
    trace, memory, path = _application(tmp_path)
    result = _attempt("run-a", outcome="failed")
    with pytest.raises(ValueError, match="durable"):
        memory.observe_recorded("run-a", result.attempt_id, project_scope="project-a")
    _record(trace, result)
    indexed = memory.observe_recorded("run-a", result.attempt_id, project_scope="project-a")
    assert memory.observe_recorded("run-a", result.attempt_id, project_scope="project-a") == indexed
    with pytest.raises(ValueError, match="conflicting"):
        memory.observe_recorded("run-a", result.attempt_id, project_scope="different-project")
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT * FROM strategy_experience").fetchone()
        encoded = repr(row)
        assert "never-index-this" not in encoded
        assert "private rationale" not in encoded
        assert "project-a" not in encoded
        assert "src/module.py" not in encoded
        assert "run-a" not in encoded
        assert "attempt-1" not in encoded
        assert "test_failure" in encoded

    # A newly composed memory service reads the same canonical index.
    restored = StrategyMemoryService(
        StrategyTraceService(SQLiteRuntimeCheckpointRepository(tmp_path / "runtime.sqlite", seal_key=b"k" * 32)),
        lambda: UnitOfWorkAdapter(str(path)),
    )
    chosen = restored.select_for_attempt(
        "run-recovery", "attempt-1", project_scope="project-a", plan=_plan(48),
        failure_class=FailureClass.TEST_FAILURE, family="patch", language="py",
        verifier_fingerprint="f" * 64,
    )
    assert len(chosen.references) == 1
    assert chosen.references[0].experience_id == indexed.experience_id
    assert chosen.references[0].stage is LearningStage.CANDIDATE
    assert chosen.context_selection.used == 48
    assert not restored.select_for_attempt("other", "attempt", project_scope="project-b", plan=_plan()).references
    with pytest.raises(ValueError, match="before"):
        restored.select_for_attempt("run-a", "attempt-1", project_scope="project-a", plan=_plan())
    with pytest.raises(ValueError, match="budget"):
        restored.select_for_attempt("run-recovery", "attempt-1", project_scope="project-a", plan=_plan(0))


def test_failed_reuse_halves_confidence_without_claiming_causality(tmp_path):
    trace, memory, _ = _application(tmp_path)
    successful = _attempt("first")
    _record(trace, successful)
    first = memory.observe_recorded("first", "attempt-1", project_scope="project-a")
    selected = memory.select_for_attempt(
        "retry", "attempt-1", project_scope="project-a", plan=_plan(48), outcome="succeeded",
    )
    assert selected.references[0].confidence == .30
    _record(trace, _attempt("retry", outcome="failed"))
    memory.observe_recorded("retry", "attempt-1", project_scope="project-a")
    another = memory.select_for_attempt(
        "next", "attempt-1", project_scope="project-a", plan=_plan(48), outcome="succeeded",
    )
    assert another.references[0].experience_id == first.experience_id
    assert another.references[0].confidence == .15
    assert another.references[0].stage is LearningStage.CANDIDATE
    # The prior selection can be replayed without accidentally choosing new IDs.
    assert memory.select_for_attempt("retry", "attempt-1", project_scope="project-a",
                                     plan=_plan(48)).references[0].experience_id == first.experience_id


def test_recovery_context_is_selected_before_attempt_and_project_bound(tmp_path):
    trace, memory, path = _application(tmp_path)
    previous = _attempt("previous", outcome="failed")
    _record(trace, previous)
    stored = memory.observe_recorded("previous", previous.attempt_id, project_scope="project-a")

    context = memory.recovery_context(
        "recovery", "attempt-1", project_scope="project-a", plan=_plan(192),
        failure_class=FailureClass.TEST_FAILURE, family="patch", language="py",
    )
    assert [ref.experience_id for ref in context.selection.references] == [stored.experience_id]
    assert context.selection.references[0].provenance == ("attempt:" + stored.attempt_digest,)
    assert context.selection.references[0].verified is False
    brief = json.loads(context.prompt_brief)
    assert brief["authority"] == "advisory_only"
    assert brief["references"][0]["id"] == stored.experience_id
    assert brief["references"][0]["stage"] == "candidate"
    assert len(context.prompt_brief.encode()) <= 192 * 2
    assert "never-index-this" not in context.prompt_brief
    assert "private rationale" not in context.prompt_brief
    assert not memory.recovery_context("empty", "attempt-1", project_scope="project-a",
                                       plan=_plan(96)).prompt_brief
    with pytest.raises(ValueError, match="another project"):
        memory.select_for_attempt("recovery", "attempt-1", project_scope="project-b", plan=_plan(192))

    _record(trace, _attempt("recovery", outcome="failed"))
    memory.observe_recorded("recovery", "attempt-1", project_scope="project-a")
    with UnitOfWorkAdapter(str(path)) as scope:
        assert scope.strategy_experiences.failed_reuses(stored.experience_id) == 1


def test_irrelevant_same_project_memory_does_not_displace_or_get_reuse_credit(tmp_path):
    trace, memory, path = _application(tmp_path)
    matched = _attempt("matched", outcome="failed")
    _record(trace, matched)
    matched_ref = memory.observe_recorded("matched", "attempt-1", project_scope="project-a")
    before = memory.recovery_context(
        "control", "attempt-1", project_scope="project-a", plan=_plan(192),
        failure_class=FailureClass.TEST_FAILURE, family="patch", language="py",
        verifier_fingerprint="f" * 64,
    )
    unrelated = replace(
        _attempt("unrelated", outcome="failed"),
        failure=FailureObservation(FailureClass.DEPENDENCY_FAILURE, evidence_digest="e" * 64),
    )
    _record(trace, unrelated)
    unrelated_ref = memory.observe_recorded("unrelated", "attempt-1", project_scope="project-a")
    after = memory.recovery_context(
        "treatment", "attempt-1", project_scope="project-a", plan=_plan(192),
        failure_class=FailureClass.TEST_FAILURE, family="patch", language="py",
        verifier_fingerprint="f" * 64,
    )
    assert after.prompt_brief == before.prompt_brief
    assert [ref.experience_id for ref in after.selection.references] == [matched_ref.experience_id]
    assert unrelated_ref.experience_id not in after.prompt_brief
    _record(trace, _attempt("treatment", outcome="failed"))
    memory.observe_recorded("treatment", "attempt-1", project_scope="project-a")
    with UnitOfWorkAdapter(str(path)) as unit:
        assert unit.strategy_experiences.failed_reuses(matched_ref.experience_id) == 1
        assert unit.strategy_experiences.failed_reuses(unrelated_ref.experience_id) == 0


def test_host_language_metadata_indexes_digest_only_codegen_scope_with_legacy_fallback(tmp_path):
    trace, memory, _ = _application(tmp_path)
    assert language_from_path("src/module.py") == "py"
    assert language_from_path("opaque.name") == "unknown"
    digest_scope = ("project:" + "e" * 64,)
    older = replace(_attempt("legacy"), signature=replace(_attempt("legacy").signature,
                                                          target_scope=digest_scope))
    newer = replace(_attempt("host-indexed"), signature=replace(_attempt("host-indexed").signature,
                                                                target_scope=digest_scope))
    _record(trace, older)
    old_row = memory.observe_recorded("legacy", "attempt-1", project_scope="project-a")
    assert old_row.language == "unknown"
    _record(trace, newer)
    indexed = memory.observe_recorded("host-indexed", "attempt-1", project_scope="project-a",
                                      language=language_from_path("src/module.py"))
    assert indexed.language == "py"
    selected = memory.select_for_attempt("recovery", "attempt-1", project_scope="project-a",
                                         plan=_plan(96), language="py", family="patch")
    assert {ref.language for ref in selected.references} == {"unknown", "py"}
    assert {ref.experience_id for ref in selected.references} == {old_row.experience_id, indexed.experience_id}
    with pytest.raises(ValueError, match="language"):
        memory.observe_recorded("host-indexed", "attempt-1", project_scope="project-a",
                                language="untrusted-language")


def test_authenticated_independent_verifiers_advance_only_existing_ladder(tmp_path):
    trace, memory, path = _application(tmp_path)
    indexed = []
    for run_id, principal in (("verified-a", "principal-a"), ("verified-b", "principal-b")):
        receipt, observation = _verified(run_id, principal=principal)
        with UnitOfWorkAdapter(str(path)) as unit:
            unit.verifier_observations.append(receipt, observation)
        attempt = _attempt(run_id, evidence=(EvidenceRef("verifier", "receipt:" + receipt.receipt_id, receipt.receipt_digest),))
        _record(trace, attempt)
        indexed.append(memory.observe_recorded(
            run_id, "attempt-1", project_scope="project-a",
            verifier_observation_id=observation.observation_id,
        ))
    selected = memory.select_for_attempt("next", "attempt-1", project_scope="project-a",
                                         plan=_plan(96), outcome="succeeded")
    assert {ref.experience_id for ref in selected.references} == {item.experience_id for item in indexed}
    assert all(ref.stage is LearningStage.FACT for ref in selected.references)
    assert all(ref.confidence == .80 for ref in selected.references)
    assert all(ref.verified and ref.provenance[1].startswith("verifier:observation-")
               for ref in selected.references)
    # An ordinary verifier receipt is not held-out strategy-policy evaluation.
    assert all(ref.stage < LearningStage.POLICY for ref in selected.references)

    forged = _attempt("forged", evidence=(EvidenceRef("verifier", "receipt:forged", indexed[0].evidence_digests[0]),))
    _record(trace, forged)
    with pytest.raises(PermissionError, match="bound"):
        memory.observe_recorded("forged", "attempt-1", project_scope="project-a",
                                verifier_observation_id="observation-" + indexed[0].evidence_digests[0])
    with pytest.raises(PermissionError, match="unavailable"):
        memory.observe_recorded("forged", "attempt-1", project_scope="project-a",
                                verifier_observation_id="observation-" + "f" * 64)
