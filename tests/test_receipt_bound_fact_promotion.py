"""Receipt-bound promotion of explicit host fact claims (ISSUE-513 chain).

Positive paths use the real producer, the sealed host verifier authority, the
producer-authorized SQLite repository and the composed authoritative fact
source.  Forged pairs are only ever fed through an in-memory repository stub,
because the persisted repository refuses them at the first insert.
"""
from dataclasses import replace
import hashlib
import json

import pytest

from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    SQLiteAuthoritativeFactSource,
)
from sonder_runtime.adapters.persistence.sqlite.verifier_observations import (
    SQLiteVerifierObservationRepository,
)
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.application.memory.facade import MemoryLearningFacade
from sonder_runtime.application.memory.learning_ladder import LearningStage
from sonder_runtime.application.memory.receipt_bound_fact_promotion import (
    FactPromotionRequest,
    ReceiptBoundFactPromotion,
)
from sonder_runtime.application.memory.receipt_observation import (
    ReceiptObservationProducer,
    VerifierReceipt,
    _receipt_payload,
)
from sonder_runtime.application.ports.host_final import HostFinalFacts
from sonder_runtime.bootstrap.managed_learning import ManagedLearningRecorder
from sonder_runtime.bootstrap.managed_standalone import ManagedStandaloneSession
from tests.test_receipt_observation import (
    _authorize,
    _eligibility,
    _evidence,
    _failed_eligibility,
)
from tests.test_receipt_observation_review import _bound


PROJECT = "repo-a"
CLAIM = "fact: the host requires the launch gate"


def _h(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _with_claim(evidence, claim=CLAIM, digest=None):
    return replace(evidence, facts=replace(
        evidence.facts,
        verified_fact_claim=claim,
        verified_fact_claim_digest=_h(claim) if digest is None else digest,
    ))


def _claim_eligibility(principal, run_id, *, claim=CLAIM, worker=None):
    return _eligibility(
        _with_claim(_evidence(principal=principal, run_id=run_id, project=PROJECT), claim),
        worker_id=worker or "lane-" + principal,
    )


def _failed_claim_eligibility(*, worker="lane-b", claim=CLAIM):
    value = _failed_eligibility(worker_id=worker, project=PROJECT)
    return _authorize(replace(value, evidence=_with_claim(value.evidence, claim)))


def _fact_id(claim=CLAIM):
    return ReceiptBoundFactPromotion.fact_id_for_claim(PROJECT, claim)


def _request(observation_ids, *, claim=CLAIM, fact_id=None):
    return FactPromotionRequest(
        PROJECT, fact_id or _fact_id(claim), claim, tuple(observation_ids),
    )


@pytest.fixture
def composed(tmp_path):
    source = SQLiteAuthoritativeFactSource("node-a", project_scope=PROJECT)
    db_path = str(tmp_path / "memory.db")

    def unit_of_work():
        return UnitOfWorkAdapter(db_path, authoritative_fact_source=source)

    def persist(eligibility):
        pair = ReceiptObservationProducer.from_terminal_eligibility(eligibility)
        with unit_of_work() as scope:
            SQLiteVerifierObservationRepository(scope.connection).append(*pair)
        return pair

    def fact_rows():
        with unit_of_work() as scope:
            return [tuple(row) for row in scope.connection.execute(
                "SELECT id, text FROM facts ORDER BY id"
            ).fetchall()]

    return MemoryLearningFacade(unit_of_work), unit_of_work, persist, fact_rows, source


# Producer and host facts -------------------------------------------------
def test_host_explicit_fact_claim_is_bound_to_receipt_and_persisted_observation():
    receipt, observation = ReceiptObservationProducer.from_terminal_eligibility(
        _claim_eligibility("principal-a", "run-a")
    )
    assert observation.content == CLAIM
    assert receipt.claim_digest == _h(CLAIM)
    assert "claim_digest:" + receipt.claim_digest in observation.provenance
    assert receipt.authorization.matches(receipt, observation)


def test_invalid_host_fact_claim_digest_fails_closed():
    with pytest.raises(PermissionError, match="claim digest is invalid"):
        ReceiptObservationProducer.from_terminal_eligibility(_eligibility(
            _with_claim(_evidence(project=PROJECT), digest="0" * 64)
        ))


def test_claim_free_receipt_keeps_its_pre_claim_serialized_shape():
    # A receipt persisted before claim binding existed must replay byte for
    # byte, so an absent claim is never serialized as null.
    receipt, observation = ReceiptObservationProducer.from_terminal_eligibility(
        _eligibility(_evidence(project=PROJECT))
    )
    assert receipt.claim_digest is None
    assert "claim_digest" not in _receipt_payload(receipt)
    assert not any(item.startswith("claim_digest:") for item in observation.provenance)
    assert observation.content.startswith("verified-subject:")


def test_host_final_facts_bound_the_explicit_claim():
    base = dict(tools=(), project_scope=PROJECT, mutation_observed=False,
                validation_attempted=True, validation_passed=True, terminal_class="NORMAL")
    with pytest.raises(ValueError, match="supplied together"):
        HostFinalFacts(**base, verified_fact_claim=CLAIM)
    with pytest.raises(ValueError, match="supplied together"):
        HostFinalFacts(**base, verified_fact_claim_digest=_h(CLAIM))
    with pytest.raises(ValueError, match="SHA-256"):
        HostFinalFacts(**base, verified_fact_claim=CLAIM, verified_fact_claim_digest="Z" * 64)
    with pytest.raises(ValueError, match="bounded"):
        HostFinalFacts(**base, verified_fact_claim="x" * 513, verified_fact_claim_digest=_h("x" * 513))
    token = "verified-subject:" + "a" * 64
    with pytest.raises(ValueError, match="subject token"):
        HostFinalFacts(**base, verified_fact_claim=token, verified_fact_claim_digest=_h(token))


# Composed promotion ------------------------------------------------------
def test_two_independent_host_claim_receipts_promote_the_claim(composed):
    application, _uow, persist, fact_rows, _source = composed
    first = persist(_claim_eligibility("principal-a", "run-a"))
    result = application.promote_verified_claim(_request((first[1].observation_id,)))
    assert result.status == "candidate"
    assert fact_rows() == []

    second = persist(_claim_eligibility("principal-b", "run-b"))
    result = application.promote_verified_claim(
        _request((first[1].observation_id, second[1].observation_id))
    )
    assert result.status == "promoted"
    assert result.decision.stage == LearningStage.FACT
    assert fact_rows() == [(_fact_id(), CLAIM)]


def test_one_principal_cannot_promote_a_claim_with_repeated_receipts(composed):
    application, _uow, persist, fact_rows, _source = composed
    ids = tuple(
        persist(_claim_eligibility("principal-a", run_id, worker=worker))[1].observation_id
        for run_id, worker in (("run-a", "lane-a"), ("run-b", "lane-b"))
    )
    result = application.promote_verified_claim(_request(ids))
    assert result.status == "candidate"
    assert fact_rows() == []


def test_persisted_verified_negative_claim_demotes_the_fact(composed):
    application, uow, persist, fact_rows, _source = composed
    positive = tuple(
        persist(_claim_eligibility(principal, run_id))[1].observation_id
        for principal, run_id in (("principal-a", "run-a"), ("principal-b", "run-b"))
    )
    assert application.promote_verified_claim(_request(positive)).status == "promoted"

    persist(_failed_claim_eligibility())
    result = application.promote_verified_claim(_request(positive))
    assert result.status == "demoted"
    assert result.decision.contradiction_count == 1
    assert fact_rows() == []
    with uow() as scope:
        tombstoned = scope.connection.execute(
            "SELECT tombstoned FROM memory_authoritative_fact_state WHERE fact_id=?",
            (_fact_id(),),
        ).fetchone()
    assert tuple(tombstoned) == (1,)


def test_subject_token_receipts_cannot_promote_semantic_text(composed):
    application, _uow, persist, fact_rows, _source = composed
    ids = tuple(
        persist(_eligibility(
            _evidence(principal=principal, run_id=run_id, project=PROJECT),
            worker_id="lane-" + principal,
        ))[1].observation_id
        for principal, run_id in (("principal-a", "run-a"), ("principal-b", "run-b"))
    )
    with pytest.raises(PermissionError, match="content is not bound"):
        application.promote_verified_claim(_request(ids))
    with pytest.raises(ValueError, match="generic verifier subject"):
        _request(ids, claim="verified-subject:" + "a" * 64, fact_id="x")
    assert fact_rows() == []


def test_claim_fact_identity_is_reserved_and_never_overwrites(composed):
    application, uow, persist, fact_rows, source = composed
    ids = tuple(
        persist(_claim_eligibility(principal, run_id))[1].observation_id
        for principal, run_id in (("principal-a", "run-a"), ("principal-b", "run-b"))
    )
    with uow() as scope:
        source.add_fact(scope.connection, "unrelated-fact", PROJECT, "keep me")
    with pytest.raises(PermissionError, match="reserved"):
        application.promote_verified_claim(_request(ids, fact_id="unrelated-fact"))
    with uow() as scope:
        source.add_fact(scope.connection, _fact_id(), PROJECT, "occupying text")
    with pytest.raises(PermissionError, match="occupied"):
        application.promote_verified_claim(_request(ids))
    assert sorted(fact_rows()) == sorted([("unrelated-fact", "keep me"), (_fact_id(), "occupying text")])
    # The reserved identity is project-scoped: the same claim elsewhere differs.
    assert ReceiptBoundFactPromotion.fact_id_for_claim("repo-b", CLAIM) != _fact_id()


def test_promotion_requires_the_observation_snapshot_transaction(composed):
    _application, uow, persist, _rows, source = composed
    pair = persist(_claim_eligibility("principal-a", "run-a"))
    with uow() as scope:
        assert not scope.connection.in_transaction
        with pytest.raises(PermissionError, match="snapshot transaction"):
            ReceiptBoundFactPromotion().apply(
                _request((pair[1].observation_id,)),
                repository=scope.verifier_observations,
                fact_source=source,
                connection=scope.connection,
            )


# Forged inputs (stub repository) -----------------------------------------
class _Repository:
    def __init__(self, *pairs):
        self.rows = {observation.observation_id: (receipt, observation) for receipt, observation in pairs}

    def get(self, observation_id):
        return self.rows.get(observation_id)

    def list_subject_pairs(self, project, content, *, limit=10_001):
        return tuple(self.rows.values())[:limit]

    def list_subject_negative_pairs(self, project, content, *, limit=16):
        return ()


def _apply_stub(composed, request, *pairs):
    _application, uow, _persist, _rows, source = composed
    with uow() as scope:
        scope.connection.execute("BEGIN IMMEDIATE")
        return ReceiptBoundFactPromotion().apply(
            request, repository=_Repository(*pairs), fact_source=source,
            connection=scope.connection,
        )


def test_receipt_claim_digest_must_match_the_requested_claim(composed):
    receipt, observation = ReceiptObservationProducer.from_terminal_eligibility(
        _claim_eligibility("principal-a", "run-a")
    )
    forged = VerifierReceipt(**{
        **_receipt_payload(receipt), "claim_digest": _h("a different claim"),
    })
    with pytest.raises(PermissionError, match="explicit host fact claim"):
        _apply_stub(composed, _request((observation.observation_id,)), (forged, observation))


def test_out_of_scope_or_unbound_observation_cannot_promote(composed):
    pair = ReceiptObservationProducer.from_terminal_eligibility(
        _claim_eligibility("principal-a", "run-a")
    )
    with pytest.raises(PermissionError, match="content is not bound"):
        _apply_stub(composed, _request((pair[1].observation_id,), claim="fact: unrelated"), pair)
    other = ReceiptObservationProducer.from_terminal_eligibility(_eligibility(
        _with_claim(_evidence(principal="principal-b", run_id="run-b", project="repo-b")),
        worker_id="lane-b",
    ))
    with pytest.raises(PermissionError, match="scope"):
        _apply_stub(composed, _request((other[1].observation_id,)), other)


def test_untrusted_label_on_a_snapshot_row_blocks_promotion(composed):
    first = ReceiptObservationProducer.from_terminal_eligibility(
        _claim_eligibility("principal-a", "run-a")
    )
    second = ReceiptObservationProducer.from_terminal_eligibility(
        _claim_eligibility("principal-b", "run-b")
    )
    relabeled = (second[0], replace(second[1], source="attributed"))
    with pytest.raises(PermissionError, match="only scoped authenticated claim observations"):
        _apply_stub(composed, _request((first[1].observation_id,)), first, relabeled)


# Live recorder routing ---------------------------------------------------
def test_recorder_promotes_host_claim_receipts_through_the_composed_facade(composed):
    application, uow, _persist, fact_rows, _source = composed

    class Application:
        memory = application
        unit_of_work = staticmethod(uow)

    outcomes = []
    for principal, run_id in (("principal-a", "run-a"), ("principal-b", "run-b")):
        session = ManagedStandaloneSession.__new__(ManagedStandaloneSession)
        session._application = Application()
        session.require_current = lambda: None
        value = _bound(_claim_eligibility(principal, run_id), session)
        recorder = ManagedLearningRecorder(Application(), verifier_factory=lambda *a: None)
        outcomes.append(recorder(session, value.evidence.result.receipt.turn, value))
    assert [item.status for item in outcomes] == ["persisted", "persisted"], outcomes
    assert [item.promotion for item in outcomes] == ["candidate", "promoted"], outcomes
    assert fact_rows() == [(_fact_id(), CLAIM)]
    with uow() as scope:
        receipts = [
            json.loads(row[0]) for row in scope.connection.execute(
                "SELECT receipt_json FROM verifier_learning_observations ORDER BY rowid"
            )
        ]
    assert [item["claim_digest"] for item in receipts] == [_h(CLAIM), _h(CLAIM)]
