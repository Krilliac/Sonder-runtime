"""Security-review regressions for MEM-003 receipt learning (PR #525)."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.sqlite.verifier_observations import (
    SQLiteVerifierObservationRepository,
)
from sonder_runtime.application.memory.learning_ladder import (
    LearningLadder,
    LearningObservation,
    LearningStage,
)
from sonder_runtime.application.memory.receipt_observation import (
    ReceiptObservationProducer,
    VerifiedSubjectFactPromotion,
    VerifierReceipt,
    _digest,
)
from sonder_runtime.application.ports.terminal_eligibility import (
    _issue_host_verifier_authority,
)
from sonder_runtime.bootstrap.managed_learning import ManagedLearningRecorder
from sonder_runtime.bootstrap.managed_standalone import ManagedStandaloneSession
from tests.test_receipt_observation import (
    _authorize,
    _eligibility,
    _evidence,
    _failed_eligibility,
)


def _h(value):
    return hashlib.sha256(value.encode()).hexdigest()


class _FactSource:
    def __init__(self):
        self.deleted = []

    def upsert_fact(self, connection, fact_id, project, text):
        connection.execute(
            "INSERT OR REPLACE INTO facts(id, project, text) VALUES(?,?,?)",
            (fact_id, project, text),
        )
        return True

    def delete_fact(self, connection, fact_id, project):
        self.deleted.append(fact_id)
        return connection.execute("DELETE FROM facts WHERE id=?", (fact_id,)).rowcount


def _connection():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE facts(id TEXT PRIMARY KEY, project TEXT, text TEXT)")
    return connection


# P1-1 ---------------------------------------------------------------------
def test_duck_typed_first_insert_authorization_cannot_forge_rows():
    """Reviewer repro: a stand-in with matches()->True must not insert."""

    class Yes:
        def matches(self, *args):
            return True

    repository = SQLiteVerifierObservationRepository(_connection())
    for worker in ("w1", "w2"):
        digest = _h("receipt" + worker)
        receipt = VerifierReceipt(
            receipt_id=digest, interaction_id="i", run_id="r", principal_id="p",
            project_scope="proj", workspace_scope="proj", verifier_outcome="passed",
            content_digest=_h("c"), subject_digest=_h("s"), receipt_digest=digest,
            authority_scope=_h(worker), authorization=Yes(),
        )
        observation = LearningObservation(
            observation_id="observation-" + digest,
            content="verified-subject:" + _h("s"),
            source=ReceiptObservationProducer.SOURCE,
            independent_key=_h(worker), provenance=("x",), confidence=1.0,
            observed_at=datetime.now(timezone.utc), positive=True,
            evaluation_passed=True, trusted_source=True,
        )
        with pytest.raises(PermissionError, match="producer authorization"):
            repository.append(receipt, observation)
    assert repository.list_pairs(limit=10) == ()


# P2-2 ---------------------------------------------------------------------
def test_same_principal_two_lanes_cannot_reach_fact():
    first = ReceiptObservationProducer.from_terminal_eligibility(
        _eligibility(_evidence(principal="owner", run_id="run-1"), worker_id="lane-a")
    )[1]
    second = ReceiptObservationProducer.from_terminal_eligibility(
        _eligibility(_evidence(principal="owner", run_id="run-2"), worker_id="lane-b")
    )[1]
    assert first.independent_key == second.independent_key
    assert LearningLadder().evaluate((first, second))[0].stage == LearningStage.CANDIDATE


def test_distinct_principals_remain_independent():
    first = ReceiptObservationProducer.from_terminal_eligibility(
        _eligibility(_evidence(principal="owner-a", run_id="run-1"), worker_id="lane-a")
    )[1]
    second = ReceiptObservationProducer.from_terminal_eligibility(
        _eligibility(_evidence(principal="owner-b", run_id="run-2"), worker_id="lane-a")
    )[1]
    assert LearningLadder().evaluate((first, second))[0].stage == LearningStage.FACT


# P1-2 ---------------------------------------------------------------------
def test_certified_after_return_is_refused_with_explicit_reason():
    value = _eligibility(_evidence(project="repo-a"), worker_id="lane-a")
    after_return = _authorize(replace(value, phase="certified_after_return"))
    with pytest.raises(PermissionError, match="certified_after_return"):
        ReceiptObservationProducer.from_terminal_eligibility(after_return)


# P2-1 ---------------------------------------------------------------------
def _raw_noise(connection, template, count, *, project="other-project", tag="noise"):
    receipt, observation = template
    base_receipt = json.loads(json.dumps({
        name: getattr(receipt, name)
        for name in receipt.__dataclass_fields__ if name != "authorization"
    }))
    base_observation = {
        name: getattr(observation, name) for name in observation.__dataclass_fields__
    }
    base_observation["observed_at"] = observation.observed_at.isoformat()
    base_observation["provenance"] = list(observation.provenance)
    rows = []
    for index in range(count):
        digest = _h("%s-%d" % (tag, index))
        r = dict(base_receipt, receipt_id=digest, receipt_digest=digest,
                 project_scope=project, workspace_scope=project)
        o = dict(base_observation, observation_id="observation-" + digest)
        rows.append(("observation-" + digest, digest, json.dumps(r), json.dumps(o)))
    connection.executemany(
        "INSERT INTO verifier_learning_observations"
        "(observation_id, receipt_id, receipt_json, observation_json) VALUES (?,?,?,?)",
        rows,
    )
    connection.commit()


def test_verified_negative_demotes_even_past_global_snapshot_bound():
    connection = _connection()
    repository = SQLiteVerifierObservationRepository(connection)
    positives = []
    for principal, run_id in (("owner-a", "run-a"), ("owner-b", "run-b")):
        pair = ReceiptObservationProducer.from_terminal_eligibility(
            _eligibility(_evidence(principal=principal, run_id=run_id, project="repo-a"),
                         worker_id="lane-" + principal)
        )
        repository.append(*pair)
        positives.append(pair[1].observation_id)
    fact_id = "verified-subject-fact-" + "a" * 64
    connection.execute("BEGIN IMMEDIATE")
    status, _ = VerifiedSubjectFactPromotion().apply(
        project="repo-a", fact_id=fact_id, observation_ids=tuple(positives),
        repository=repository, fact_source=_FactSource(), connection=connection,
    )
    connection.commit()
    assert status == "promoted"
    # Unrelated rows from another project exceed the old global snapshot.
    _raw_noise(connection, pair, 10_001)
    # And more same-subject rows in this project than the completeness bound:
    # the verified negative must still demote before that check.
    _raw_noise(connection, pair, 10_001, project="repo-a", tag="same-subject")
    negative = ReceiptObservationProducer.from_terminal_eligibility(
        _failed_eligibility(worker_id="lane-owner-b", project="repo-a")
    )
    repository.append(*negative)
    source = _FactSource()
    connection.execute("BEGIN IMMEDIATE")
    status, decision = VerifiedSubjectFactPromotion().apply(
        project="repo-a", fact_id=fact_id, observation_ids=tuple(positives),
        repository=repository, fact_source=source, connection=connection,
    )
    connection.commit()
    assert status == "demoted" and source.deleted == [fact_id]
    assert decision.contradiction_count == 1
    plan = " ".join(
        str(row) for row in connection.execute(
            "EXPLAIN QUERY PLAN " + repository._SUBJECT_QUERY,
            ("repo-a", "repo-a", "verified-subject:" + "a" * 64, 10),
        )
    )
    assert "verifier_learning_observations_subject" in plan


# P2-3 ---------------------------------------------------------------------
def test_recorder_resolves_terminal_eligibility_once(tmp_path):
    from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter

    db_path = str(tmp_path / "memory.db")
    resolutions = []
    sealed = {}
    value = _eligibility(_evidence(project="repo-a"), worker_id="lane-a")

    def resolver():
        resolutions.append(1)
        return sealed["value"]

    sealed["value"] = replace(value, authority=_issue_host_verifier_authority(resolver))

    class Application:
        unit_of_work = staticmethod(lambda: UnitOfWorkAdapter(db_path))

    session = ManagedStandaloneSession.__new__(ManagedStandaloneSession)
    session._application = Application()
    session.require_current = lambda: None
    recomputed = []

    def terminal_eligibility(*args, **kwargs):
        recomputed.append(1)
        return sealed["value"]

    session.terminal_eligibility = terminal_eligibility
    recorder = ManagedLearningRecorder(Application(), verifier_factory=lambda *a: None)
    outcome = recorder(session, object(), sealed["value"])
    assert outcome.status == "persisted", outcome
    assert recomputed == [] and len(resolutions) == 1


# P2-1 visibility ----------------------------------------------------------
def test_promotion_refusal_reason_is_visible(tmp_path, caplog):
    db_path = str(tmp_path / "memory.db")

    class Source:
        project_scope = "repo-a"

        def activate(self, connection):
            return None

    class Memory:
        def promote_verified_subject(self, *args):
            raise PermissionError("verifier observation snapshot is incomplete")

    class Scope:
        authoritative_fact_source = Source()

        def __enter__(self):
            self.connection = sqlite3.connect(db_path)
            return self

        def __exit__(self, *exc):
            self.connection.commit()
            self.connection.close()

        @property
        def verifier_observations(self):
            return SQLiteVerifierObservationRepository(self.connection)

    class Application:
        memory = Memory()
        unit_of_work = staticmethod(Scope)

    session = ManagedStandaloneSession.__new__(ManagedStandaloneSession)
    session._application = Application()
    session.require_current = lambda: None
    value = _eligibility(_evidence(project="repo-a"), worker_id="lane-a")
    recorder = ManagedLearningRecorder(Application(), verifier_factory=lambda *a: None)
    with caplog.at_level("WARNING"):
        outcome = recorder(session, object(), value)
    assert outcome.status == "persisted" and outcome.promotion == "refused"
    assert "snapshot is incomplete" in outcome.reason
    assert any("snapshot is incomplete" in record.getMessage() for record in caplog.records)
