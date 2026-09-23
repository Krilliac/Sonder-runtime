from dataclasses import replace
import hashlib
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.sqlite.verifier_observations import (
    SQLiteVerifierObservationRepository,
)
from sonder_runtime.application.memory.learning_ladder import LearningLadder, LearningStage
from sonder_runtime.application.memory.receipt_observation import ReceiptObservationProducer, _digest
from sonder_runtime.application.ports.host_final import HostFinalFacts
from sonder_runtime.application.ports.host_turn_links import (
    FinalizedHostResult, ManagedHostFinalEvidence, ManagedHostTerminalLink,
    ManagedHostTurnLink,
)
from sonder_runtime.application.ports.lane_continuation import PendingVerificationIdentity
from sonder_runtime.application.ports.terminal_eligibility import ManagedTerminalEligibility
from sonder_runtime.bootstrap.managed_standalone import ManagedStandaloneSession


def _evidence(*, principal="worker-a", run_id="run-1", outcome="passed", receipt_id=None):
    project = r"D:\owned\project"
    output = "host verifier certificate"
    output_digest = hashlib.sha256(output.encode()).hexdigest()
    receipt_digest = receipt_id or hashlib.sha256(
        (principal + run_id + outcome).encode()
    ).hexdigest()
    turn = ManagedHostTurnLink("continuation-1", "parent-1", "conversation-1", principal, run_id, 1)
    link = ManagedHostTerminalLink(
        turn, "original-id", "a" * 64, "final-id", "b" * 64,
        receipt_digest, output_digest,
    )
    facts = HostFinalFacts(
        (), project, outcome == "passed", True, outcome == "passed",
        "NORMAL" if outcome == "passed" else "VALIDATION_FAILED",
        certificate_id="cert-" + run_id, certificate_generation=1,
        certificate_code="verified-change", delegated_work=True,
    )
    return ManagedHostFinalEvidence(FinalizedHostResult(output, link), facts)


def _eligibility(evidence, *, worker_id="lane-worker-a", eligible=True, phase="certified", subject_digest="a" * 64):
    identity = PendingVerificationIdentity(
        "continuation-1", "verification-1", "parent-1", 1, 1,
        "b" * 64, "command-1", "a" * 64, 1,
    )
    return ManagedTerminalEligibility(
        evidence, eligible, phase, "CERTIFIED" if eligible else "FINAL_CERTIFICATE_MISMATCH",
        pending_identity=identity if eligible else None,
        authenticated_worker_id=worker_id if eligible else None,
        verified_subject_digest=subject_digest if eligible else None,
    )


def test_real_typed_host_receipt_derives_trust_and_identity_without_spoofable_fields():
    receipt, observation = ReceiptObservationProducer.from_terminal_eligibility(_eligibility(_evidence()))
    assert receipt.verifier_outcome == "passed"
    assert observation.source == "authenticated_verifier"
    assert observation.trusted_source is True
    assert observation.independent_key
    assert "worker-a" not in observation.independent_key
    assert observation.provenance[0] == "receipt:" + receipt.receipt_id
    with pytest.raises(TypeError):
        ReceiptObservationProducer.from_terminal_eligibility(_evidence(), source="attributed")


def test_one_authenticated_worker_cannot_create_independence_from_repeated_receipts():
    first = ReceiptObservationProducer.from_terminal_eligibility(_eligibility(_evidence(run_id="run-1")))[1]
    second = ReceiptObservationProducer.from_terminal_eligibility(_eligibility(_evidence(run_id="run-2")))[1]
    assert first.independent_key == second.independent_key
    decision = LearningLadder().evaluate((first, second))[0]
    assert decision.stage == LearningStage.CANDIDATE


def test_distinct_authenticated_workers_can_reach_fact_but_contradiction_demotes():
    first = ReceiptObservationProducer.from_terminal_eligibility(_eligibility(_evidence(principal="owner"), worker_id="lane-worker-a"))[1]
    second = ReceiptObservationProducer.from_terminal_eligibility(_eligibility(_evidence(principal="owner", run_id="run-2"), worker_id="lane-worker-b"))[1]
    assert LearningLadder().evaluate((first, second))[0].stage == LearningStage.FACT
    failure = {
        "schema": "delegated-verification-failure-v1",
        "failed_check": {"target": "unit"},
        "failed_proof": {"status": "failed", "exit_code": 7},
        "before_manifest_digest": "d" * 64,
        "after_manifest_digest": "d" * 64,
    }
    failure["receipt_digest"] = _digest(failure)
    negative = replace(
        _eligibility(
            _evidence(principal="owner", run_id="run-3", outcome="failed"),
            worker_id="lane-worker-b",
        ),
        phase="failed",
        eligible=False,
        code="CHECK_FAILED",
        verified_failure_receipt=failure,
    )
    receipt, negative_observation = ReceiptObservationProducer.from_terminal_eligibility(negative)
    assert receipt.verifier_outcome == "failed"
    assert negative_observation.positive is False
    assert negative_observation.trusted_source is True
    decisions = LearningLadder().evaluate((first, second, negative_observation))
    assert decisions[0].stage == LearningStage.CANDIDATE
    assert decisions[0].contradiction_count == 1


def test_negative_receipt_requires_specific_immutable_failed_check_proof():
    eligibility = replace(
        _eligibility(_evidence(principal="owner", run_id="run-negative")),
        phase="failed",
        eligible=False,
        code="CHECK_FAILED",
        verified_failure_receipt={
            "schema": "delegated-verification-failure-v1",
            "failed_check": {"target": "unit"},
            "failed_proof": {"status": "succeeded", "exit_code": 0},
            "before_manifest_digest": "f" * 64,
            "after_manifest_digest": "f" * 64,
        },
    )
    bad = dict(eligibility.verified_failure_receipt)
    bad["receipt_digest"] = _digest({k: v for k, v in bad.items() if k != "receipt_digest"})
    eligibility = replace(eligibility, verified_failure_receipt=bad)
    with pytest.raises(PermissionError, match="immutable verifier failure receipt is invalid"):
        ReceiptObservationProducer.from_terminal_eligibility(eligibility)


def test_unrelated_verified_subjects_never_aggregate():
    first = ReceiptObservationProducer.from_terminal_eligibility(
        _eligibility(_evidence(principal="owner", run_id="run-1"), worker_id="lane-worker-a", subject_digest="a" * 64)
    )[1]
    second = ReceiptObservationProducer.from_terminal_eligibility(
        _eligibility(_evidence(principal="owner", run_id="run-2"), worker_id="lane-worker-b", subject_digest="b" * 64)
    )[1]
    decisions = LearningLadder().evaluate((first, second))
    assert len(decisions) == 2
    assert all(decision.stage == LearningStage.CANDIDATE for decision in decisions)


def test_uncertain_receipt_cannot_mint_positive_trusted_observation():
    evidence = _evidence(outcome="uncertain")
    evidence = replace(evidence, facts=replace(evidence.facts, validation_attempted=False, terminal_class="UNVERIFIED"))
    with pytest.raises(PermissionError, match="current certified terminal eligibility"):
        ReceiptObservationProducer.from_terminal_eligibility(
            _eligibility(evidence, eligible=False, phase="unknown")
        )


def test_sqlite_repository_is_restart_replay_idempotent_and_immutable(tmp_path):
    path = tmp_path / "memory.db"
    evidence = _evidence()
    receipt, observation = ReceiptObservationProducer.from_terminal_eligibility(_eligibility(evidence))
    first = sqlite3.connect(path)
    repo = SQLiteVerifierObservationRepository(first)
    assert repo.append(receipt, observation) == observation
    assert repo.append(receipt, observation) == observation
    first.close()

    second = sqlite3.connect(path)
    reopened = SQLiteVerifierObservationRepository(second)
    assert reopened.get(observation.observation_id)[1] == observation
    altered = replace(observation, content="spoofed")
    with pytest.raises(ValueError, match="conflicting verifier receipt replay"):
        reopened.append(receipt, altered)
    with pytest.raises(sqlite3.IntegrityError):
        second.execute("DELETE FROM verifier_learning_observations")
    second.close()


def test_observation_append_respects_caller_transaction_and_conflict_savepoint(tmp_path):
    path = tmp_path / "memory.db"
    receipt, observation = ReceiptObservationProducer.from_terminal_eligibility(
        _eligibility(_evidence())
    )
    connection = sqlite3.connect(path)
    repository = SQLiteVerifierObservationRepository(connection)
    connection.execute("CREATE TABLE caller_marker(value TEXT NOT NULL)")
    connection.commit()

    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO caller_marker(value) VALUES('retained')")
    repository.append(receipt, observation)
    with pytest.raises(ValueError, match="conflicting verifier receipt replay"):
        repository.append(receipt, replace(observation, content="different"))
    assert connection.execute("SELECT value FROM caller_marker").fetchone()[0] == "retained"
    connection.rollback()
    assert repository.get(observation.observation_id) is None
    assert connection.execute("SELECT COUNT(*) FROM caller_marker").fetchone()[0] == 0
    connection.close()


def test_host_session_persists_only_after_current_eligibility():
    evidence = _evidence()
    eligibility = _eligibility(evidence, worker_id="lane-worker-a")
    session = ManagedStandaloneSession.__new__(ManagedStandaloneSession)
    session.terminal_eligibility = lambda expected_turn, verifier_factory: eligibility

    class Repository:
        def __init__(self):
            self.rows = []

        def append(self, receipt, observation):
            self.rows.append((receipt, observation))

    repository = Repository()
    observed = session.persist_learning_observation(
        object(), verifier_factory=object(), repository=repository
    )
    assert observed == repository.rows[0][1]
