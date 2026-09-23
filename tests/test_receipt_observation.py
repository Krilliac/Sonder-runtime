from dataclasses import replace
import hashlib
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.sqlite.verifier_observations import (
    SQLiteVerifierObservationRepository,
)
from sonder_runtime.application.memory.learning_ladder import LearningLadder, LearningStage
from sonder_runtime.application.memory.receipt_observation import ReceiptObservationProducer
from sonder_runtime.application.ports.host_final import HostFinalFacts
from sonder_runtime.application.ports.host_turn_links import (
    FinalizedHostResult, ManagedHostFinalEvidence, ManagedHostTerminalLink,
    ManagedHostTurnLink,
)


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


def test_real_typed_host_receipt_derives_trust_and_identity_without_spoofable_fields():
    receipt, observation = ReceiptObservationProducer.from_host_final(_evidence())
    assert receipt.verifier_outcome == "passed"
    assert observation.source == "authenticated_verifier"
    assert observation.trusted_source is True
    assert observation.independent_key
    assert "worker-a" not in observation.independent_key
    assert observation.provenance[0] == "receipt:" + receipt.receipt_id
    with pytest.raises(TypeError):
        ReceiptObservationProducer.from_host_final(_evidence(), source="attributed")


def test_one_authenticated_worker_cannot_create_independence_from_repeated_receipts():
    first = ReceiptObservationProducer.from_host_final(_evidence(run_id="run-1"))[1]
    second = ReceiptObservationProducer.from_host_final(_evidence(run_id="run-2"))[1]
    assert first.independent_key == second.independent_key
    decision = LearningLadder().evaluate((first, second))[0]
    assert decision.stage == LearningStage.CANDIDATE


def test_distinct_authenticated_workers_can_reach_fact_but_contradiction_demotes():
    first = ReceiptObservationProducer.from_host_final(_evidence(principal="worker-a"))[1]
    second = ReceiptObservationProducer.from_host_final(_evidence(principal="worker-b", run_id="run-2"))[1]
    assert LearningLadder().evaluate((first, second))[0].stage == LearningStage.FACT
    negative = ReceiptObservationProducer.from_host_final(
        _evidence(principal="worker-b", run_id="run-3", outcome="failed")
    )[1]
    decision = LearningLadder().evaluate((first, second, negative))[0]
    assert decision.stage == LearningStage.CANDIDATE
    assert decision.contradiction_count == 1


def test_uncertain_receipt_cannot_mint_positive_trusted_observation():
    evidence = _evidence(outcome="uncertain")
    evidence = replace(evidence, facts=replace(evidence.facts, validation_attempted=False, terminal_class="UNVERIFIED"))
    _, observation = ReceiptObservationProducer.from_host_final(evidence)
    assert observation.positive is False
    assert observation.trusted_source is False
    assert observation.confidence == 0.0


def test_sqlite_repository_is_restart_replay_idempotent_and_immutable(tmp_path):
    path = tmp_path / "memory.db"
    evidence = _evidence()
    receipt, observation = ReceiptObservationProducer.from_host_final(evidence)
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
