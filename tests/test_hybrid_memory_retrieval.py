from datetime import datetime, timedelta, timezone

from sonder_runtime.application.memory.hybrid_retrieval import (
    HybridMemoryRetriever, MemoryCandidate, RetrievalQuery,
)
from sonder_runtime.application.memory.memory_policy import MemoryClass, TemporalTruth
from sonder_runtime.adapters import embeddings, memory_store, recall


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


def memory(memory_id, text, *, kind=MemoryClass.WORKING, created=NOW, **kwargs):
    return MemoryCandidate(memory_id, text, kind, created, **kwargs)


def test_exact_mode_is_bounded_and_deterministic():
    rows = [
        memory("b", "release the lock safely", created=NOW),
        memory("a", "release the lock safely", created=NOW),
        memory("other", "unrelated advice", created=NOW),
    ]
    result = HybridMemoryRetriever().retrieve(rows, RetrievalQuery("lock release", mode="exact", limit=2))
    assert [item.candidate.memory_id for item in result] == ["a", "b"]
    assert all(item.exact_score > 0 for item in result)


def test_temporal_mode_filters_invalid_memory_and_applies_decay():
    rows = [
        memory("current", "policy", kind=MemoryClass.SEMANTIC, temporal=TemporalTruth(NOW - timedelta(days=1), confidence=1.0), confidence=1.0),
        memory("future", "policy", kind=MemoryClass.SEMANTIC, temporal=TemporalTruth(NOW + timedelta(days=1), confidence=1.0), confidence=1.0),
    ]
    result = HybridMemoryRetriever().retrieve(rows, RetrievalQuery("policy", mode="temporal", at=NOW, scope="project"))
    assert [item.candidate.memory_id for item in result] == ["current"]


def test_decision_and_failure_modes_require_the_requested_tag():
    rows = [
        memory("decision", "choose sqlite", kind=MemoryClass.PROJECT, decision_tags=("sqlite",), confidence=1.0),
        memory("failure", "sqlite lock race", kind=MemoryClass.FAILURE, failure_tags=("sqlite-lock",), confidence=1.0),
    ]
    retriever = HybridMemoryRetriever()
    assert [item.candidate.memory_id for item in retriever.retrieve(rows, RetrievalQuery("sqlite", mode="decision", decision_tag="sqlite", scope="project"))] == ["decision"]
    assert [item.candidate.memory_id for item in retriever.retrieve(rows, RetrievalQuery("sqlite", mode="failure", failure_tag="sqlite-lock", scope="project"))] == ["failure"]


def test_entity_mode_returns_only_the_requested_entity():
    rows = [
        memory("repo-a", "DuetOS repository", kind=MemoryClass.PROJECT, entity_id="repo:duetos"),
        memory("repo-b", "SparkEngine repository", kind=MemoryClass.PROJECT, entity_id="repo:spark"),
    ]
    result = HybridMemoryRetriever().retrieve(
        rows, RetrievalQuery("repository", mode="entity", entity_id="repo:duetos", scope="project")
    )
    assert [item.candidate.memory_id for item in result] == ["repo-a"]


def test_stale_memory_is_excluded_unless_explicitly_requested():
    stale = memory("stale", "old fact", kind=MemoryClass.SEMANTIC, freshness=0.0, confidence=1.0)
    retriever = HybridMemoryRetriever()
    assert retriever.retrieve([stale], RetrievalQuery("old fact", scope="project")) == ()
    assert len(retriever.retrieve([stale], RetrievalQuery("old fact", scope="project", include_stale=True))) == 1


def test_project_scoped_memory_requires_exact_project_even_for_semantic_lookup():
    scoped = memory(
        "private-project", "deployment secret", kind=MemoryClass.PROJECT,
        project="project-a", confidence=1.0, semantic_score=1.0,
    )
    retriever = HybridMemoryRetriever()
    assert retriever.retrieve([scoped], RetrievalQuery("deployment", scope="project")) == ()
    assert retriever.retrieve([scoped], RetrievalQuery("deployment", scope="project", project="project-b")) == ()
    assert len(retriever.retrieve(
        [scoped], RetrievalQuery("deployment", scope="project", project="project-a")
    )) == 1


def test_live_recall_uses_hybrid_exact_order_with_project_sqlite_scope():
    conn = memory_store.connect(":memory:")
    for identifier, task, response, project in (
        ("a", "bounded timeout handling", "project A", "project-a"),
        ("b", "generic network handling", "project B", "project-a"),
        ("c", "bounded timeout handling", "other project", "project-b"),
    ):
        memory_store.log_interaction(
            conn, identifier, task, "", response, "code", project=project,
            task_embedding=embeddings.to_blob([1.0, 0.0]),
            task_embedding_model=embeddings.EMBED_IDENTITY,
            task_embedding_revision=embeddings.EMBED_REVISION,
            task_embedding_dim=2,
        )
        memory_store.record_outcome_row(conn, identifier, "tests_passed", 1.0, source="caller")

    scoped = recall.recall(
        conn, "bounded timeout", k=2, qv=[1.0, 0.0], min_sim=0.0,
        project="project-a",
    )

    assert scoped[0] == "bounded timeout handling -> project A"
    assert all("other project" not in item for item in scoped)
    assert len(recall.recall(
        conn, "", k=2, qv=[1.0, 0.0], min_sim=0.0,
        project="project-a",
    )) == 2
    assert recall.recall(
        conn, "bounded " * 800, k=1, qv=[1.0, 0.0], min_sim=0.0,
        project="project-a",
    )
