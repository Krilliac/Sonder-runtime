import embeddings
import memory_store as ms
import recall
import sqlite3
import time
import pytest


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
    ms.record_outcome_row(c, iid, "tests_passed", 1.0, source="caller")


def _bulk_good(c, rows):
    c.executemany(
        "INSERT INTO interactions("
        "id,task,response,tier,project,project_explicit,session_id,task_embedding,"
        "task_embedding_model,task_embedding_revision,task_embedding_dim) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                row[0], row[1], row[2], "code", row[4], 1,
                row[7] if len(row) > 7 else None,
                embeddings.to_blob(row[3]), row[5], row[6], len(row[3]),
            )
            for row in rows
        ],
    )
    c.executemany(
        "INSERT INTO outcomes(interaction_id,signal,reward,source) "
        "VALUES(?,?,?,'caller')",
        [(row[0], "tests_passed", 1.0) for row in rows],
    )
    c.execute("UPDATE interactions SET ts=printf('%020d',rowid)")
    c.commit()


def test_recall_returns_similar_good_solution():
    c = _conn()
    _store_good(c, "i1", "reverse a string", "def rev(s): return s[::-1]", [1.0, 0.0])
    _store_good(c, "i2", "parse json", "import json", [0.0, 1.0])
    # query embedding aligned with i1
    out = recall.recall(c, "reverse text", k=2, embed_fn=lambda t: [1.0, 0.0], min_sim=0.9)
    assert out == ["reverse a string -> def rev(s): return s[::-1]"]


def test_recall_skips_corrupt_embedding_blob_without_disclosure():
    c = _conn()
    _store_good(c, "i1", "task", "response", [1.0, 0.0])
    c.execute(
        "UPDATE interactions SET task_embedding = ? WHERE id = ?",
        (b"not-a-float-vector", "i1"),
    )

    assert recall.recall(
        c, "q", qv=[1.0, 0.0], min_sim=0.5,
        embedding_model=embeddings.EMBED_IDENTITY,
        embedding_revision=embeddings.EMBED_REVISION,
    ) == []


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
    ms.record_outcome_row(c, "i1", "failed", -1.0, source="caller")
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
    ms.record_outcome_row(c, "conflict", "failed", -1.0, source="caller")

    assert recall.recall(
        c, "task", qv=[1.0, 0.0], min_sim=0.5,
    ) == []

    _store_good(c, "unknown", "other task", "response", [1.0, 0.0])
    c.execute(
        "INSERT INTO outcomes(interaction_id,signal,reward,source) "
        "VALUES(?,?,?,'caller')",
        ("unknown", "future_signal", 99.0),
    )
    c.commit()
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
    ms.record_outcome_row(c, "legacy", "tests_passed", 1.0, source="caller")

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


def test_recall_closes_code_fence_opened_by_truncation():
    """An unterminated ``` would swallow the "# Task:" header that follows."""
    c = _conn()
    long_resp = "Here is the fix:\n\n```cpp\n" + "int x = 0;\n" * 60 + "```\n"
    _store_good(c, "i1", "task", long_resp, [1.0, 0.0])
    out = recall.recall(c, "q", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5)
    fences = [line for line in out[0].splitlines() if line.lstrip().startswith("```")]
    assert len(fences) % 2 == 0, out[0][-120:]
    assert out[0].rstrip().endswith("```")


def test_recall_truncation_leaves_fence_free_text_alone():
    c = _conn()
    _store_good(c, "i1", "task", "y" * 1000, [1.0, 0.0])
    out = recall.recall(c, "q", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5)
    assert "```" not in out[0]
    assert out[0].endswith("…")


def test_recall_does_not_open_a_fence_the_prefix_already_neutralized():
    """A ``` on the response's FIRST line lands after "task -> " and so opens
    nothing; appending a closer there would open a block instead of closing
    one. 63% of live truncate-length responses start with a fence."""
    c = _conn()
    _store_good(c, "i1", "task", "```py\n" + "z = 1\n" * 100, [1.0, 0.0])
    out = recall.recall(c, "q", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5)
    assert not out[0].rstrip().endswith("```")
    fences = [line for line in out[0].splitlines() if line.lstrip().startswith("```")]
    assert fences == []


def test_recall_window_is_bounded_newest_first_with_truthful_cursor():
    c = _conn()
    rows = [
        ("old-best", "old exact task", "old result", [1.0, 0.0], None,
         "embed-v2", "rev-v2"),
        ("older-far", "older far", "far", [0.0, 1.0], None,
         "embed-v2", "rev-v2"),
        ("window-edge", "window edge exact", "edge result", [1.0, 0.0], None,
         "embed-v2", "rev-v2"),
    ]
    rows.extend(
        ("far-%03d" % index, "far", "far", [0.0, 1.0], None,
         "embed-v2", "rev-v2")
        for index in range(ms.RECALL_CANDIDATE_ROW_LIMIT - 1)
    )
    _bulk_good(c, rows)

    first = recall.recall_page(
        c, "exact", k=5, qv=[1.0, 0.0], min_sim=0.9,
        embedding_model="embed-v2", embedding_revision="rev-v2",
    )
    assert first.results == ("window edge exact -> edge result",)
    assert first.incomplete is True
    assert first.termination == "row_limit"
    assert first.candidates_examined == ms.RECALL_CANDIDATE_ROW_LIMIT
    assert first.candidates_scored == ms.RECALL_CANDIDATE_ROW_LIMIT
    assert isinstance(first.next_cursor, str)
    assert first.next_cursor.startswith("r1.")

    second = recall.recall_page(
        c, "exact", k=5, qv=[1.0, 0.0], min_sim=0.9,
        embedding_model="embed-v2", embedding_revision="rev-v2",
        candidate_cursor=first.next_cursor,
    )
    assert second.results == ("old exact task -> old result",)
    assert second.incomplete is False
    assert second.next_cursor is None

    # The compatibility API remains a plain list and never implies that the
    # older global best was scored.
    assert recall.recall(
        c, "exact", k=5, qv=[1.0, 0.0], min_sim=0.9,
        embedding_model="embed-v2", embedding_revision="rev-v2",
    ) == ["window edge exact -> edge result"]


def test_project_and_embedding_filters_apply_before_candidate_cap():
    c = _conn()
    _bulk_good(c, [(
        "target", "target task", "target result", [1.0, 0.0], "project-b",
        "embed-v2", "rev-v2",
    )])
    _bulk_good(c, [
        (
            "noise-%04d" % index, "noise", "noise", [0.0, 1.0],
            "project-a", "wrong-model", "wrong-revision",
        )
        for index in range(ms.RECALL_CANDIDATE_ROW_LIMIT + 100)
    ])

    page = recall.recall_page(
        c, "target", qv=[1.0, 0.0], min_sim=0.9, project="project-b",
        embedding_model="embed-v2", embedding_revision="rev-v2",
    )
    assert page.results == ("target task -> target result",)
    assert page.incomplete is False
    assert page.candidates_examined == 1


def test_excluded_session_applies_before_candidate_cap():
    c = _conn()
    _bulk_good(c, [(
        "target", "target task", "target result", [1.0, 0.0], "project",
        "embed-v2", "rev-v2", "other-session",
    )])
    _bulk_good(c, [
        (
            "current-%04d" % index, "target task", "current", [1.0, 0.0],
            "project", "embed-v2", "rev-v2", "current-session",
        )
        for index in range(ms.RECALL_CANDIDATE_ROW_LIMIT + 25)
    ])

    page = recall.recall_page(
        c, "target", qv=[1.0, 0.0], min_sim=0.9, project="project",
        exclude_session="current-session",
        embedding_model="embed-v2", embedding_revision="rev-v2",
    )
    assert page.results == ("target task -> target result",)
    assert page.incomplete is False
    assert page.candidates_examined == 1


def test_corrupt_rows_do_not_starve_valid_bounded_candidates():
    c = _conn()
    _store_good(
        c, "valid", "valid task", "valid result", [1.0, 0.0],
        embedding_model="embed-v2", embedding_revision="rev-v2",
    )
    c.execute(
        "INSERT INTO interactions("
        "id,task,response,tier,task_embedding,task_embedding_model,"
        "task_embedding_revision,task_embedding_dim) VALUES(?,?,?,?,?,?,?,?)",
        (
            "corrupt", b"not text", b"not text", "code", b"x" * 100_000,
            "embed-v2", "rev-v2", 25_000,
        ),
    )
    c.execute(
        "INSERT INTO outcomes(interaction_id,signal,reward,source) "
        "VALUES(?,?,?,'caller')",
        ("corrupt", "tests_passed", 1.0),
    )
    c.execute(
        "INSERT INTO interactions("
        "id,task,response,tier,task_embedding,task_embedding_model,"
        "task_embedding_revision,task_embedding_dim) "
        "VALUES('invalid-utf8',CAST(x'80' AS TEXT),'response','code',?,?,?,?)",
        (
            embeddings.to_blob([1.0, 0.0]), "embed-v2", "rev-v2", 2,
        ),
    )
    c.execute(
        "INSERT INTO outcomes(interaction_id,signal,reward,source) "
        "VALUES(?,?,?,'caller')",
        ("invalid-utf8", "tests_passed", 1.0),
    )
    c.commit()

    page = recall.recall_page(
        c, "valid", qv=[1.0, 0.0], min_sim=0.9,
        embedding_model="embed-v2", embedding_revision="rev-v2",
    )
    assert page.results == ("valid task -> valid result",)
    assert page.candidates_examined == 2


def test_noncanonical_or_non_numeric_rewards_fail_closed_before_the_cap():
    c = _conn()
    for interaction_id in ("valid", "text-reward", "wrong-reward"):
        _store_good(
            c, interaction_id, interaction_id + " task", "result", [1.0, 0.0],
            embedding_model="embed-v2", embedding_revision="rev-v2",
        )
    c.execute(
        "UPDATE outcomes SET reward='not-a-number' "
        "WHERE interaction_id='text-reward'"
    )
    c.execute(
        "UPDATE outcomes SET reward=0.99 WHERE interaction_id='wrong-reward'"
    )
    c.commit()

    page = recall.recall_page(
        c, "valid", qv=[1.0, 0.0], min_sim=0.9,
        embedding_model="embed-v2", embedding_revision="rev-v2",
    )

    assert page.results == ("valid task -> result",)
    assert page.candidates_examined == 1


def test_candidate_byte_and_time_limits_are_explicit_and_connection_recovers():
    c = _conn()
    _bulk_good(c, [
        (
            "row-%02d" % index, "task-" + "x" * 100, "response", [1.0, 0.0],
            None, "embed-v2", "rev-v2",
        )
        for index in range(10)
    ])

    byte_page = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2, byte_limit=300,
    )
    assert byte_page.incomplete is True
    assert byte_page.termination == "byte_limit"
    assert byte_page.bytes_loaded <= 300
    assert byte_page.next_cursor is not None

    timed_page = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2, time_limit_s=0,
    )
    assert timed_page.incomplete is True
    assert timed_page.termination == "time_limit"
    assert timed_page.rows == ()
    assert c.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 10

    cancelled_page = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2, cancel_check=lambda: True,
    )
    assert cancelled_page.incomplete is True
    assert cancelled_page.termination == "cancelled"
    assert cancelled_page.rows == ()
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000


def test_candidate_cursor_pages_are_exclusive_stable_and_non_overlapping():
    c = _conn()
    _bulk_good(c, [
        (
            "page-%02d" % index, "task", "result", [1.0, 0.0], None,
            "embed-v2", "rev-v2",
        )
        for index in range(8)
    ])
    cursor = None
    seen = []
    terminations = []
    while True:
        page = ms.good_interaction_candidate_page(
            c, embedding_model="embed-v2", embedding_revision="rev-v2",
            embedding_dim=2, cursor=cursor, row_limit=3,
        )
        seen.extend(row["id"] for row in page.rows)
        terminations.append(page.termination)
        if not page.incomplete:
            break
        assert page.next_cursor is not None
        cursor = page.next_cursor

    assert seen == ["page-%02d" % index for index in reversed(range(8))]
    assert len(seen) == len(set(seen)) == 8
    assert terminations == ["row_limit", "row_limit", "complete"]


def test_candidate_cursor_rejects_malformed_and_noncanonical_values():
    c = _conn()
    _bulk_good(c, [(
        "one", "task", "result", [1.0, 0.0], None,
        "embed-v2", "rev-v2",
    )])
    valid = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2, row_limit=1,
    )
    # Exactly one row means complete/no cursor, so make a canonical token via
    # the storage-owned encoder for non-canonical mutation coverage.
    canonical = ms._encode_recall_cursor("00000000000000000001", "one")
    for invalid in (
        1, "", "r1.", "r1.not-base64!", canonical + "=", "r2." + canonical[3:],
        "r1." + "a" * (ms.RECALL_CURSOR_MAX_CHARS + 1),
    ):
        with pytest.raises(ValueError, match="cursor is invalid"):
            ms.good_interaction_candidate_page(c, cursor=invalid)
    assert valid.incomplete is False


def test_candidate_cursor_round_trips_maximum_unicode_ordering_keys():
    c = _conn()
    pathological_id = "\U0001f642" * 256
    pathological_ts = "\U0001f642" * 64
    _bulk_good(c, [
        ("older", "task", "result", [1.0, 0.0], None, "embed-v2", "rev-v2"),
        (
            pathological_id, "task", "result", [1.0, 0.0], None,
            "embed-v2", "rev-v2",
        ),
    ])
    c.execute(
        "UPDATE interactions SET ts=? WHERE id=?",
        (pathological_ts, pathological_id),
    )
    c.commit()

    first = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2, row_limit=1,
    )
    assert first.incomplete is True
    assert first.rows[0]["id"] == pathological_id
    assert first.next_cursor is not None
    assert len(first.next_cursor) <= ms.RECALL_CURSOR_MAX_CHARS + 3

    second = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2, cursor=first.next_cursor, row_limit=1,
    )
    assert tuple(row["id"] for row in second.rows) == ("older",)


def test_empty_corrupt_interaction_id_is_not_a_cursor_candidate():
    c = _conn()
    _bulk_good(c, [(
        "", "task", "result", [1.0, 0.0], None, "embed-v2", "rev-v2",
    )])

    page = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2,
    )

    assert page.rows == ()
    assert page.rows_examined == 0


def test_timestamp_id_cursor_is_safe_when_sqlite_reuses_rowids():
    c = _conn()
    _bulk_good(c, [
        (
            "old-%02d" % index, "task", "result", [1.0, 0.0], None,
            "embed-v2", "rev-v2",
        )
        for index in range(6)
    ])
    first = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2, row_limit=3,
    )
    assert first.incomplete is True
    assert first.next_cursor is not None

    c.execute("DELETE FROM outcomes")
    c.execute("DELETE FROM interactions")
    c.execute(
        "INSERT INTO interactions("
        "id,task,response,tier,ts,task_embedding,task_embedding_model,"
        "task_embedding_revision,task_embedding_dim) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "new-rowid-one", "task", "new result", "code",
            "99999999999999999999", embeddings.to_blob([1.0, 0.0]),
            "embed-v2", "rev-v2", 2,
        ),
    )
    c.execute(
        "INSERT INTO outcomes(interaction_id,signal,reward,source) "
        "VALUES(?,?,?,'caller')",
        ("new-rowid-one", "tests_passed", 1.0),
    )
    c.commit()
    assert c.execute(
        "SELECT rowid FROM interactions WHERE id='new-rowid-one'"
    ).fetchone()[0] == 1

    older = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2, cursor=first.next_cursor, row_limit=3,
    )
    assert older.rows == ()
    assert older.incomplete is False


def test_project_query_plan_uses_recall_index_without_temp_sort():
    c = _conn()
    _bulk_good(c, [(
        "one", "task", "result", [1.0, 0.0], "project",
        "embed-v2", "rev-v2",
    )])
    statements = []
    c.set_trace_callback(
        lambda statement: statements.append(statement)
        if "candidate_ts" in statement else None
    )
    ms.good_interaction_candidate_page(
        c, project="project", embedding_model="embed-v2",
        embedding_revision="rev-v2", embedding_dim=2,
    )
    c.set_trace_callback(None)
    query = statements[-1]
    plan = "\n".join(
        row[3] for row in c.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    )
    assert "idx_interactions_recall_project" in plan
    assert "idx_outcomes_interaction_signal_reward" in plan
    assert "USE TEMP B-TREE" not in plan


def test_existing_database_stamp_installs_recall_indexes_idempotently():
    c = _conn()
    c.execute("DROP INDEX idx_interactions_recall_project")
    c.execute("DROP INDEX idx_interactions_recall_global")
    c.execute("PRAGMA user_version=1")
    c.commit()

    ms.init_db(c)
    ms.init_db(c)

    indexes = {
        row[0]
        for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_interactions_recall_project" in indexes
    assert "idx_interactions_recall_global" in indexes
    assert c.execute("PRAGMA user_version").fetchone()[0] == ms._schema_stamp()


def test_global_query_plan_uses_recall_index_without_temp_sort():
    c = _conn()
    _bulk_good(c, [(
        "one", "task", "result", [1.0, 0.0], "project",
        "embed-v2", "rev-v2",
    )])
    statements = []
    c.set_trace_callback(
        lambda statement: statements.append(statement)
        if "candidate_ts" in statement else None
    )
    ms.good_interaction_candidate_page(
        c, include_all_projects=True, embedding_model="embed-v2",
        embedding_revision="rev-v2", embedding_dim=2,
    )
    c.set_trace_callback(None)
    query = statements[-1]
    plan = "\n".join(
        row[3] for row in c.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    )
    assert "idx_interactions_recall_global" in plan
    assert "idx_outcomes_interaction_signal_reward" in plan
    assert "USE TEMP B-TREE" not in plan


def test_candidate_lock_wait_is_bounded_and_busy_timeout_is_restored(tmp_path):
    path = tmp_path / "locked-recall.sqlite3"
    reader = ms.connect(str(path))
    _bulk_good(reader, [(
        "one", "task", "result", [1.0, 0.0], None,
        "embed-v2", "rev-v2",
    )])
    # DELETE journal mode makes an EXCLUSIVE transaction block readers too,
    # exercising the busy-handler path rather than only the VM progress hook.
    assert reader.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    locker = sqlite3.connect(str(path), timeout=1)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        page = ms.good_interaction_candidate_page(
            reader, embedding_model="embed-v2", embedding_revision="rev-v2",
            embedding_dim=2, time_limit_s=0.05,
        )
        elapsed = time.monotonic() - started
    finally:
        locker.rollback()
        locker.close()

    assert page.incomplete is True
    assert page.termination == "time_limit"
    assert page.rows == ()
    assert elapsed < 0.5
    assert reader.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
    assert reader.execute("SELECT COUNT(*) FROM interactions").fetchone()[0] == 1


def test_corrupt_ordering_key_never_returns_a_non_advancing_cursor():
    c = _conn()
    _bulk_good(c, [
        ("valid", "task", "result", [1.0, 0.0], None, "embed-v2", "rev-v2"),
        ("corrupt", "task", "result", [1.0, 0.0], None, "embed-v2", "rev-v2"),
    ])
    # SQLite can contain invalid UTF-8 in a value tagged TEXT when corruption
    # or a non-Python writer bypasses the normal adapter.
    c.execute("UPDATE interactions SET ts=CAST(X'FF' AS TEXT) WHERE id='corrupt'")
    c.commit()

    page = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2, row_limit=1,
    )

    assert page.incomplete is True
    assert page.termination == "row_limit"
    assert page.next_cursor is None


def test_large_store_request_stays_within_declared_row_and_time_budget():
    c = _conn()
    _bulk_good(c, [
        (
            "large-%05d" % index, "task", "result", [1.0, 0.0], None,
            "embed-v2", "rev-v2",
        )
        for index in range(10_000)
    ])
    started = time.monotonic()
    page = ms.good_interaction_candidate_page(
        c, embedding_model="embed-v2", embedding_revision="rev-v2",
        embedding_dim=2,
    )
    elapsed = time.monotonic() - started

    assert page.incomplete is True
    assert page.termination in {"row_limit", "time_limit"}
    assert len(page.rows) <= ms.RECALL_CANDIDATE_ROW_LIMIT
    assert page.bytes_loaded <= ms.RECALL_CANDIDATE_BYTE_LIMIT
    assert elapsed < 2.0
