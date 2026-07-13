import embeddings
import memory_store as ms
import recall


def _conn():
    return ms.connect(":memory:")


def _store_good(
    c, iid, task, response, vec, session_id=None, project=None,
    embedding_model=embeddings.EMBED_IDENTITY,
    embedding_revision=embeddings.EMBED_REVISION,
):
    ms.log_interaction(c, iid, task, "", response, "sonder",
                       session_id=session_id, task_embedding=embeddings.to_blob(vec),
                       project=project, task_embedding_model=embedding_model,
                       task_embedding_revision=embedding_revision,
                       task_embedding_dim=len(vec))
    ms.record_outcome_row(c, iid, "tests_passed", 1.0)


def test_recall_returns_similar_good_solution():
    c = _conn()
    _store_good(c, "i1", "reverse a string", "def rev(s): return s[::-1]", [1.0, 0.0])
    _store_good(c, "i2", "parse json", "import json", [0.0, 1.0])
    # query embedding aligned with i1
    out = recall.recall(c, "reverse text", k=2, embed_fn=lambda t: [1.0, 0.0], min_sim=0.9)
    assert len(out) == 1
    assert "reverse a string" in out[0]
    assert "s[::-1]" in out[0]


def test_recall_respects_min_sim_threshold():
    c = _conn()
    _store_good(c, "i1", "task", "resp", [1.0, 0.0])
    # orthogonal query -> cosine 0 -> below threshold
    assert recall.recall(c, "q", embed_fn=lambda t: [0.0, 1.0], min_sim=0.5) == []


def test_recall_soft_fails_when_no_embeddings():
    c = _conn()
    _store_good(c, "i1", "task", "resp", [1.0, 0.0])
    assert recall.recall(c, "q", embed_fn=lambda t: None) == []


def test_recall_excludes_current_session():
    c = _conn()
    _store_good(c, "i1", "task", "resp", [1.0, 0.0], session_id="cur")
    out = recall.recall(c, "q", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5,
                        exclude_session="cur")
    assert out == []


def test_recall_ignores_bad_outcomes():
    c = _conn()
    ms.log_interaction(c, "i1", "task", "", "resp", "sonder",
                       task_embedding=embeddings.to_blob([1.0, 0.0]))
    ms.record_outcome_row(c, "i1", "failed", -1.0)
    assert recall.recall(c, "q", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5) == []


def test_recall_truncates_long_responses():
    c = _conn()
    long_resp = "x" * 1000
    _store_good(c, "i1", "task", long_resp, [1.0, 0.0])
    out = recall.recall(c, "q", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5)
    assert len(out[0]) < 1000
    assert out[0].endswith("…")


def test_recall_is_project_scoped_unless_global_override_is_explicit():
    c = _conn()
    _store_good(
        c, "a", "same task", "PROJECT_A_PRIVATE", [1.0, 0.0],
        project="project-a",
    )
    _store_good(
        c, "b", "same task", "project B solution", [1.0, 0.0],
        project="project-b",
    )

    scoped = recall.recall(
        c, "same task", k=2, qv=[1.0, 0.0], min_sim=0.5,
        project="project-b",
    )
    global_rows = recall.recall(
        c, "same task", k=2, qv=[1.0, 0.0], min_sim=0.5,
        project="project-b", include_all_projects=True,
    )

    assert scoped == ["same task -> project B solution"]
    assert len(global_rows) == 2
    assert any("PROJECT_A_PRIVATE" in row for row in global_rows)

    string_false = recall.recall(
        c, "same task", k=2, qv=[1.0, 0.0], min_sim=0.5,
        project="project-b", include_all_projects="false",
    )
    assert string_false == ["same task -> project B solution"]


def test_recall_quarantines_ambiguous_migrated_session_project():
    c = _conn()
    ms.touch_session(c, "legacy-session", project="project-a")
    _store_good(
        c, "legacy", "same task", "legacy scoped solution", [1.0, 0.0],
        session_id="legacy-session",
    )
    c.execute(
        "UPDATE interactions SET project_explicit=0 WHERE id='legacy'"
    )
    c.commit()

    assert recall.recall(
        c, "same task", qv=[1.0, 0.0], min_sim=0.5,
        project="project-a",
    ) == []
    assert recall.recall(
        c, "same task", qv=[1.0, 0.0], min_sim=0.5,
        project="project-b",
    ) == []


def test_recall_vetoes_interaction_with_contradictory_outcome():
    c = _conn()
    _store_good(c, "conflict", "task", "response", [1.0, 0.0])
    ms.record_outcome_row(c, "conflict", "failed", -1.0)

    assert recall.recall(
        c, "task", qv=[1.0, 0.0], min_sim=0.5,
    ) == []

    _store_good(c, "unknown", "other task", "response", [1.0, 0.0])
    ms.record_outcome_row(c, "unknown", "future_signal", 99.0)
    assert recall.recall(
        c, "other task", qv=[1.0, 0.0], min_sim=0.5,
    ) == []


def test_recall_skips_wrong_embedding_model_with_same_dimension():
    c = _conn()
    _store_good(
        c, "old", "task", "old private solution", [1.0, 0.0],
        embedding_model="embed-v1",
    )
    _store_good(
        c, "current", "task", "current solution", [1.0, 0.0],
        embedding_model="embed-v2",
    )

    rows = recall.recall(
        c, "task", qv=[1.0, 0.0], min_sim=0.5,
        embedding_model="embed-v2",
    )

    assert rows == ["task -> current solution"]


def test_recall_fails_closed_for_legacy_vector_without_provenance():
    c = _conn()
    ms.log_interaction(
        c, "legacy", "task", "", "legacy response", "sonder",
        task_embedding=embeddings.to_blob([1.0, 0.0]),
    )
    ms.record_outcome_row(c, "legacy", "tests_passed", 1.0)

    assert recall.recall(
        c, "task", qv=[1.0, 0.0], min_sim=0.5,
    ) == []


def test_recall_fails_closed_for_missing_dimension_or_zero_norm():
    c = _conn()
    _store_good(c, "missing-dim", "task", "response", [1.0, 0.0])
    c.execute(
        "UPDATE interactions SET task_embedding_dim=NULL WHERE id='missing-dim'"
    )
    _store_good(c, "zero", "task", "response", [1.0, 0.0])
    c.execute(
        "UPDATE interactions SET task_embedding=? WHERE id='zero'",
        (embeddings.to_blob([0.0, 0.0]),),
    )
    c.commit()

    assert recall.recall(
        c, "task", qv=[1.0, 0.0], min_sim=0.5,
    ) == []


def test_recall_unversioned_runtime_rejects_hashed_revision():
    c = _conn()
    _store_good(
        c, "stale", "task", "response", [1.0, 0.0],
        embedding_model="embed-v2", embedding_revision="stale-hash",
    )

    assert recall.recall(
        c, "task", qv=[1.0, 0.0], min_sim=0.5,
        embedding_model="embed-v2", embedding_revision="",
    ) == []
