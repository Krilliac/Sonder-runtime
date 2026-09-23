from dataclasses import replace
import hashlib
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.sqlite.verifier_observations import (
    SQLiteVerifierObservationRepository,
)
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    SQLiteAuthoritativeFactSource,
)
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.application.memory.facade import MemoryLearningFacade
from sonder_runtime.application.memory.learning_ladder import LearningLadder, LearningStage
from sonder_runtime.application.memory.receipt_observation import ReceiptObservationProducer, _digest
from sonder_runtime.application.ports.host_final import HostFinalFacts
from sonder_runtime.application.ports.host_turn_links import (
    FinalizedHostResult, ManagedHostFinalEvidence, ManagedHostTerminalLink,
    ManagedHostTurnLink,
)
from sonder_runtime.application.ports.lane_continuation import PendingVerificationIdentity
from sonder_runtime.application.ports.terminal_eligibility import ManagedTerminalEligibility
from sonder_runtime.application.ports.terminal_eligibility import _issue_host_verifier_authority
from sonder_runtime.bootstrap.managed_standalone import ManagedStandaloneSession


def _evidence(*, principal="worker-a", run_id="run-1", outcome="passed", receipt_id=None, project=r"D:\owned\project"):
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
    value = ManagedTerminalEligibility(
        evidence, eligible, phase, "CERTIFIED" if eligible else "FINAL_CERTIFICATE_MISMATCH",
        pending_identity=identity if eligible else None,
        authenticated_worker_id=worker_id if eligible else None,
        verified_subject_digest=subject_digest if eligible else None,
    )
    return _authorize(value)


def _authorize(value):
    holder = {}
    authority = _issue_host_verifier_authority(lambda: holder["value"])
    holder["value"] = replace(
        value,
        authority=authority,
    )
    return holder["value"]


def _failed_eligibility(*, worker_id="lane-worker-a", run_id="run-failed", project="repo-a"):
    failure = {
        "schema": "delegated-verification-failure-v1",
        "failed_check": {"target": "unit"},
        "failed_proof": {"status": "failed", "exit_code": 7},
        "before_manifest_digest": "d" * 64,
        "after_manifest_digest": "d" * 64,
    }
    failure["receipt_digest"] = _digest(failure)
    return _authorize(replace(
        _eligibility(
            _evidence(
                principal="owner", run_id=run_id, outcome="failed",
                project=project,
            ),
            worker_id=worker_id,
        ),
        phase="failed",
        eligible=False,
        code="CHECK_FAILED",
        verified_failure_receipt=failure,
    ))


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


def test_public_eligibility_without_owner_authority_cannot_mint_trusted_evidence():
    value = _eligibility(_evidence())
    forged = replace(value, authority=None, authenticated_worker_id="forged-worker")
    with pytest.raises(PermissionError, match="owner-bound managed verifier authority"):
        ReceiptObservationProducer.from_terminal_eligibility(forged)


def test_duck_typed_public_authority_cannot_mint_trusted_evidence():
    value = _eligibility(_evidence())

    class ForgedAuthority:
        def resolve(self):
            return value

    forged = replace(value, authority=ForgedAuthority())
    with pytest.raises(PermissionError, match="owner-bound managed verifier authority"):
        ReceiptObservationProducer.from_terminal_eligibility(forged)


def test_modified_public_fields_are_ignored_when_owner_authority_is_present():
    value = _eligibility(_evidence(), worker_id="real-worker")
    modified = replace(
        value,
        authenticated_worker_id="forged-worker",
        verified_subject_digest="f" * 64,
    )
    receipt, observation = ReceiptObservationProducer.from_terminal_eligibility(modified)
    assert receipt.authority_scope == _digest({
        "worker_id": "real-worker", "workspace_scope": r"D:\owned\project",
    })
    assert "forged-worker" not in observation.independent_key


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
    negative = _authorize(replace(
        _eligibility(
            _evidence(principal="owner", run_id="run-3", outcome="failed"),
            worker_id="lane-worker-b",
        ),
        phase="failed",
        eligible=False,
        code="CHECK_FAILED",
        verified_failure_receipt=failure,
    ))
    receipt, negative_observation = ReceiptObservationProducer.from_terminal_eligibility(negative)
    assert receipt.verifier_outcome == "failed"
    assert negative_observation.positive is False
    assert negative_observation.trusted_source is True
    decisions = LearningLadder().evaluate((first, second, negative_observation))
    assert decisions[0].stage == LearningStage.CANDIDATE
    assert decisions[0].contradiction_count == 1


def test_negative_receipt_requires_specific_immutable_failed_check_proof():
    eligibility = _authorize(replace(
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
    ))
    bad = dict(eligibility.verified_failure_receipt)
    bad["receipt_digest"] = _digest({k: v for k, v in bad.items() if k != "receipt_digest"})
    eligibility = _authorize(replace(eligibility, verified_failure_receipt=bad))
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
            _authorize(_eligibility(evidence, eligible=False, phase="unknown"))
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


def test_first_repository_insert_requires_producer_authorization(tmp_path):
    path = tmp_path / "memory.db"
    receipt, observation = ReceiptObservationProducer.from_terminal_eligibility(
        _eligibility(_evidence())
    )
    connection = sqlite3.connect(path)
    repo = SQLiteVerifierObservationRepository(connection)
    with pytest.raises(PermissionError, match="producer authorization"):
        repo.append(replace(receipt, authorization=None), observation)
    assert repo.append(receipt, observation) == observation
    connection.close()


def test_first_insert_authorization_binds_every_receipt_and_observation_field(tmp_path):
    receipt, observation = ReceiptObservationProducer.from_terminal_eligibility(
        _eligibility(_evidence())
    )
    receipt_mutations = (
        {"interaction_id": "forged-interaction"},
        {"run_id": "forged-run"},
        {"principal_id": "forged-principal"},
        {"project_scope": "forged-project"},
        {"workspace_scope": "forged-workspace"},
        {"verifier_outcome": "failed"},
        {"content_digest": "f" * 64},
        {"subject_digest": "e" * 64},
        {"authority_scope": "d" * 64},
    )
    observation_mutations = (
        {"content": "forged-content"},
        {"source": "attributed"},
        {"independent_key": "forged-worker"},
        {"provenance": ("forged-provenance",)},
        {"positive": False},
        {"evaluation_passed": False},
        {"trusted_source": False},
    )
    connection = sqlite3.connect(tmp_path / "memory.db")
    repo = SQLiteVerifierObservationRepository(connection)
    for changes in receipt_mutations:
        with pytest.raises(PermissionError, match="producer authorization"):
            repo.append(replace(receipt, **changes), observation)
    for changes in observation_mutations:
        with pytest.raises(PermissionError, match="producer authorization"):
            repo.append(receipt, replace(observation, **changes))
    assert repo.append(receipt, observation) == observation
    connection.close()


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


def test_managed_session_durable_observation_uses_application_unit_of_work(tmp_path):
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    db_path = str(tmp_path / "memory.db")

    class Application:
        unit_of_work = staticmethod(
            lambda: UnitOfWorkAdapter(db_path, authoritative_fact_source=source)
        )

    eligibility = _eligibility(
        _evidence(project="repo-a"), worker_id="lane-a"
    )
    session = ManagedStandaloneSession.__new__(ManagedStandaloneSession)
    session._application = Application()
    session.terminal_eligibility = lambda expected_turn, verifier_factory: eligibility
    observed = session.persist_learning_observation_durable(
        object(), verifier_factory=object()
    )
    assert observed.observation_id
    with UnitOfWorkAdapter(db_path, authoritative_fact_source=source) as scope:
        stored = SQLiteVerifierObservationRepository(scope.connection).get(
            observed.observation_id
        )
    assert stored is not None
    assert stored[1] == observed


def test_application_composition_persists_and_promotes_independent_subject_receipts(tmp_path):
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    db_path = str(tmp_path / "memory.db")
    application = MemoryLearningFacade(
        lambda: UnitOfWorkAdapter(db_path, authoritative_fact_source=source)
    )
    produced = []
    for worker, run_id in (("lane-a", "run-a"), ("lane-b", "run-b")):
        receipt, observation = ReceiptObservationProducer.from_terminal_eligibility(
            _eligibility(
                _evidence(principal="owner", run_id=run_id, project="repo-a"),
                worker_id=worker,
            )
        )
        with UnitOfWorkAdapter(db_path, authoritative_fact_source=source) as scope:
            SQLiteVerifierObservationRepository(scope.connection).append(receipt, observation)
        produced.append(observation.observation_id)

    status, decision = application.promote_verified_subject(
        "repo-a", "verified-subject-fact", tuple(produced)
    )
    assert status == "promoted"
    assert decision.stage == LearningStage.FACT
    with UnitOfWorkAdapter(db_path, authoritative_fact_source=source) as scope:
        facts = scope.memory.facts_for_project("repo-a")
    assert facts[0]["text"] == "verified-subject:" + "a" * 64


def test_application_promotion_demotes_on_persisted_verified_negative(tmp_path):
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    db_path = str(tmp_path / "memory.db")
    application = MemoryLearningFacade(
        lambda: UnitOfWorkAdapter(db_path, authoritative_fact_source=source)
    )
    positive = []
    for worker, run_id in (("lane-a", "run-a"), ("lane-b", "run-b")):
        pair = ReceiptObservationProducer.from_terminal_eligibility(
            _eligibility(
                _evidence(principal="owner", run_id=run_id, project="repo-a"),
                worker_id=worker,
            )
        )
        with UnitOfWorkAdapter(db_path, authoritative_fact_source=source) as scope:
            SQLiteVerifierObservationRepository(scope.connection).append(*pair)
        positive.append(pair[1].observation_id)
    status, _ = application.promote_verified_subject(
        "repo-a", "verified-subject-fact", tuple(positive)
    )
    assert status == "promoted"

    negative = ReceiptObservationProducer.from_terminal_eligibility(
        _failed_eligibility(worker_id="lane-b", project="repo-a")
    )
    with UnitOfWorkAdapter(db_path, authoritative_fact_source=source) as scope:
        SQLiteVerifierObservationRepository(scope.connection).append(*negative)
    status, decision = application.promote_verified_subject(
        "repo-a", "verified-subject-fact", (positive[0], positive[1], negative[1].observation_id)
    )
    assert status == "demoted"
    assert decision.contradiction_count == 1
