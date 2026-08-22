from __future__ import annotations

from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.application.memory.facade import MemoryLearningFacade
from sonder_runtime.application.memory.memory_policy import MemoryClass, RetrievalDisposition
from sonder_runtime.domain.memory.wp6_typed import Evidence, EvidenceKind, MemoryLabel, TypedMemory


def test_facade_recall_uses_existing_recall_service_and_transaction(tmp_path):
    seen = []

    class Recall:
        def retrieve(self, connection, task, **options):
            seen.append((connection, task, options))
            return ["bounded result"]

    db = str(tmp_path / "memory.db")
    facade = MemoryLearningFacade(lambda: UnitOfWorkAdapter(db), recall_service=Recall())

    assert facade.recall("question", k=1, project="p") == ["bounded result"]
    assert seen[0][0] is not None
    assert seen[0][1:] == ("question", {"k": 1, "project": "p"})


def test_facade_records_outcome_and_outbox_atomically(tmp_path):
    db = str(tmp_path / "memory.db")
    with UnitOfWorkAdapter(db) as uow:
        uow.memory.log_interaction("i1", "task", "", "response", "code")

    facade = MemoryLearningFacade(lambda: UnitOfWorkAdapter(db))
    assert facade.record("i1", "accepted") == 0.8

    with UnitOfWorkAdapter(db) as uow:
        event = uow.connection.execute(
            "SELECT event_type, aggregate_id, payload_json FROM outbox_events"
        ).fetchone()
    assert tuple(event)[:2] == ("outcome.recorded", "i1")
    assert '"signal": "accepted"' in event[2]


def test_facade_exposes_typed_policy_and_procedural_learning(tmp_path):
    facade = MemoryLearningFacade(lambda: UnitOfWorkAdapter(str(tmp_path / "memory.db")))
    decision = facade.evaluate_retrieval(
        "m1", MemoryClass.PROCEDURAL, requested_scope="project", confidence=0.9,
        provenance=("test-run",), freshness=1.0,
    )
    assert decision.disposition is RetrievalDisposition.ALLOW

    memory = TypedMemory(
        "m1", "Use the verified path.", MemoryLabel.PROCEDURAL,
        evidence=(Evidence(EvidenceKind.TEST_PASS, "suite"),),
    )
    assert [item.memory_id for item in facade.promotion_candidates([memory])] == ["m1"]
