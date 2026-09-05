from __future__ import annotations

from datetime import datetime, timedelta, timezone

import memory_store as ms

from sonder_runtime.adapters import embeddings, recall


def _conn():
    return ms.connect(":memory:")


def _store_good(conn, *, timestamp: str):
    ms.log_interaction(
        conn,
        "interaction-1",
        "repair the failing test",
        "",
        "updated implementation",
        "code",
        project="sonder",
        task_embedding=embeddings.to_blob([1.0, 0.0]),
        task_embedding_model="embed-v2",
        task_embedding_revision="revision-7",
        task_embedding_dim=2,
    )
    conn.execute("UPDATE interactions SET ts=? WHERE id=?", (timestamp, "interaction-1"))
    ms.record_outcome_row(conn, "interaction-1", "tests_passed", 1.0, source="machine")


def test_recall_page_returns_provenance_confidence_and_freshness_without_changing_text_api():
    now = datetime.now(timezone.utc)
    _conn_value = _conn()
    _store_good(
        _conn_value,
        timestamp=(now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
    )

    page = recall.recall_page(
        _conn_value,
        "repair test",
        qv=[1.0, 0.0],
        min_sim=0.5,
        project="sonder",
        embedding_model="embed-v2",
        embedding_revision="revision-7",
    )

    assert page.results == ("repair the failing test -> updated implementation",)
    assert len(page.items) == 1
    item = page.items[0]
    assert item.interaction_id == "interaction-1"
    assert item.memory_id == "interaction-1"
    assert item.text == page.results[0]
    assert item.score_components["semantic"] == 1.0
    assert "interaction:interaction-1" in item.provenance
    assert "outcome:tests_passed:machine" in item.provenance
    assert "embedding:embed-v2@revision-7" in item.provenance
    assert item.evidence == ("tests_passed", "machine")
    assert item.confidence == 1.0
    assert item.freshness is not None and 0.99 < item.freshness <= 1.0
    assert item.degradation_reasons == ()
    assert page.degradation_reasons == ()


def test_recall_page_reports_unknown_timestamp_and_preserves_compatibility():
    conn = _conn()
    _store_good(conn, timestamp="legacy-ordering-key")

    page = recall.recall_page(
        conn,
        "repair test",
        qv=[1.0, 0.0],
        min_sim=0.5,
        project="sonder",
        embedding_model="embed-v2",
        embedding_revision="revision-7",
    )

    assert page.results == ("repair the failing test -> updated implementation",)
    assert recall.recall(
        conn,
        "repair test",
        qv=[1.0, 0.0],
        min_sim=0.5,
        project="sonder",
        embedding_model="embed-v2",
        embedding_revision="revision-7",
    ) == list(page.results)
    assert page.items[0].freshness is None
    assert page.items[0].degradation_reasons == ("freshness_unavailable",)


def test_recall_page_explains_missing_query_embedding():
    conn = _conn()
    page = recall.recall_page(conn, "repair test", embed_fn=lambda _task: None)

    assert page.results == ()
    assert page.items == ()
    assert page.degradation_reasons == ("no_query_embedding",)
