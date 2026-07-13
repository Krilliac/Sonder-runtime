import embeddings
import memory_store as ms
import sqlite3


def _conn():
    return ms.connect(":memory:")


def test_migration_adds_session_and_embedding_columns():
    c = _conn()
    cols = ms._column_names(c, "interactions")
    assert "session_id" in cols
    assert "task_embedding" in cols
    assert "task_embedding_model" in cols
    assert "task_embedding_revision" in cols
    assert "task_embedding_dim" in cols
    assert "project" in cols
    lesson_cols = ms._column_names(c, "lessons")
    assert {"embedding_model", "embedding_revision", "embedding_dim"} <= lesson_cols


def test_log_interaction_defaults_are_session_less():
    c = _conn()
    ms.log_interaction(c, "a", "t", "", "r", "code")  # old 6-arg call style
    row = ms.get_interaction(c, "a")
    assert row["session_id"] is None
    assert row["task_embedding"] is None
    assert row["project"] is None
    assert row["project_explicit"] == 1


def test_fresh_schema_raw_insert_defaults_project_provenance_to_explicit():
    c = _conn()
    c.execute(
        "INSERT INTO interactions(id, task, response, tier) VALUES(?, ?, ?, ?)",
        ("raw", "task", "response", "code"),
    )
    c.commit()

    assert ms.get_interaction(c, "raw")["project_explicit"] == 1


def test_log_interaction_persists_project_scope():
    c = _conn()
    ms.log_interaction(
        c, "scoped", "task", "", "response", "sonder",
        project="project-a",
    )

    assert ms.get_interaction(c, "scoped")["project"] == "project-a"


def test_explicit_no_project_is_not_inferred_from_session_project():
    c = _conn()
    ms.touch_session(c, "shared", project="project-a")
    ms.log_interaction(
        c, "legacy-a", "task a", "", "response a", "sonder",
        session_id="shared", project_explicit=False,
    )
    ms.log_interaction(
        c, "explicit-none", "task none", "", "response none", "sonder",
        session_id="shared", project=None, project_explicit=True,
    )

    assert [row["id"] for row in ms.session_turns_for_project(
        c, "shared", "project-a",
    )] == []
    assert [row["id"] for row in ms.session_turns_for_project(
        c, "shared", None,
    )] == ["explicit-none"]
    assert ms.ambiguous_legacy_project_turn_count(c) == 1


def test_session_turns_ordered_oldest_first():
    c = _conn()
    ms.log_interaction(c, "i1", "q1", "", "a1", "sonder", session_id="S")
    ms.log_interaction(c, "i2", "q2", "", "a2", "sonder", session_id="S")
    ms.log_interaction(c, "i3", "q3", "", "a3", "sonder", session_id="other")
    turns = ms.session_turns(c, "S")
    assert [t["task"] for t in turns] == ["q1", "q2"]
    assert [t["id"] for t in turns] == ["i1", "i2"]


def test_project_summaries_are_isolated_with_explicit_none_scope():
    c = _conn()
    ms.update_session_project_summary(c, "shared", "a", "summary a", "a1")
    ms.update_session_project_summary(c, "shared", "b", "summary b", "b1")
    ms.update_session_project_summary(c, "shared", None, "summary none", "n1")

    assert ms.get_session_project_summary(c, "shared", "a") == {
        "summary": "summary a", "summarized_through": "a1",
    }
    assert ms.get_session_project_summary(c, "shared", "b")["summary"] == "summary b"
    assert ms.get_session_project_summary(c, "shared", None)["summary"] == "summary none"


def test_explicit_unscoped_turn_stays_unscoped_when_session_gets_project():
    c = _conn()
    ms.touch_session(c, "shared", project=None)
    ms.log_interaction(
        c, "legacy-none", "task", "", "private unscoped response", "sonder",
        session_id="shared",
    )

    ms.touch_session(c, "shared", project="project-b")

    assert [row["id"] for row in ms.session_turns_for_project(
        c, "shared", None,
    )] == ["legacy-none"]
    assert ms.session_turns_for_project(c, "shared", "project-b") == []
    row = ms.get_interaction(c, "legacy-none")
    assert row["project"] is None
    assert row["project_explicit"] == 1


def test_ambiguous_count_includes_null_marker_from_older_schema():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE interactions(session_id TEXT, project TEXT, "
        "project_explicit INTEGER)"
    )
    c.execute(
        "INSERT INTO interactions VALUES('legacy', NULL, NULL)"
    )

    assert ms.ambiguous_legacy_project_turn_count(c) == 1


def test_session_project_change_keeps_ambiguous_legacy_turn_quarantined():
    c = _conn()
    ms.touch_session(c, "shared", project="project-a")
    ms.log_interaction(
        c, "legacy-a", "task", "", "private a response", "sonder",
        session_id="shared", project_explicit=False,
    )

    ms.set_session_project(c, "shared", "project-b")

    assert [row["id"] for row in ms.session_turns_for_project(
        c, "shared", "project-a",
    )] == []
    assert ms.session_turns_for_project(c, "shared", "project-b") == []
    assert ms.session_turns_for_project(c, "shared", None) == []
    assert ms.ambiguous_legacy_project_turn_count(c) == 1
    assert ms.unscoped_session_turn_count(c) == 1


def test_session_history_caps_to_last_n():
    c = _conn()
    for i in range(5):
        ms.log_interaction(c, "i%d" % i, "q%d" % i, "", "a%d" % i, "sonder", session_id="S")
    hist = ms.session_history(c, "S", max_turns=2)
    assert hist == [("q3", "a3"), ("q4", "a4")]


def test_session_turn_count():
    c = _conn()
    assert ms.session_turn_count(c, "S") == 0
    ms.log_interaction(c, "i1", "q", "", "a", "sonder", session_id="S")
    assert ms.session_turn_count(c, "S") == 1


def test_touch_get_and_title_summary_roundtrip():
    c = _conn()
    ms.touch_session(c, "S", project="proj")
    sess = ms.get_session(c, "S")
    assert sess["session_id"] == "S"
    assert sess["project"] == "proj"
    ms.set_session_title(c, "S", "My Thread")
    ms.update_session_summary(c, "S", "did stuff", "i9")
    sess = ms.get_session(c, "S")
    assert sess["title"] == "My Thread"
    assert sess["summary"] == "did stuff"
    assert sess["summarized_through"] == "i9"


def test_touch_session_does_not_clobber_existing_project():
    c = _conn()
    ms.touch_session(c, "S", project="first")
    ms.touch_session(c, "S", project="second")
    assert ms.get_session(c, "S")["project"] == "first"


def test_list_sessions_has_turn_counts():
    c = _conn()
    ms.touch_session(c, "S")
    ms.log_interaction(c, "i1", "q", "", "a", "sonder", session_id="S")
    ms.log_interaction(c, "i2", "q", "", "a", "sonder", session_id="S")
    rows = ms.list_sessions(c)
    assert rows[0]["session_id"] == "S"
    assert rows[0]["turn_count"] == 2


def test_find_session_by_id_and_title_prefix():
    c = _conn()
    ms.touch_session(c, "abc123")
    ms.set_session_title(c, "abc123", "Refactor the parser")
    assert ms.find_session(c, "abc123") == "abc123"
    assert ms.find_session(c, "Refactor") == "abc123"
    assert ms.find_session(c, "nope") is None


def test_good_interactions_with_embeddings_filters_and_excludes():
    c = _conn()
    vector = embeddings.to_blob([1.0, 0.0])
    ms.log_interaction(c, "g", "task g", "", "resp", "sonder",
                       session_id="A", task_embedding=vector)
    ms.log_interaction(c, "bad", "task bad", "", "resp", "sonder",
                       task_embedding=vector)
    ms.log_interaction(c, "noemb", "task noemb", "", "resp", "sonder")
    ms.record_outcome_row(c, "g", "tests_passed", 1.0)
    ms.record_outcome_row(c, "bad", "failed", -1.0)
    ms.record_outcome_row(c, "noemb", "tests_passed", 1.0)
    rows = ms.good_interactions_with_embeddings(c)
    assert {r["id"] for r in rows} == {"g"}  # only good + has embedding
    # excluding session A removes it
    assert ms.good_interactions_with_embeddings(c, exclude_session="A") == []


def test_facts_add_list_count():
    c = _conn()
    ms.add_fact(c, "f1", "proj", "uses MSVC", b"\x00")
    ms.add_fact(c, "f2", "proj", "tabs not spaces", None)
    ms.add_fact(c, "f3", "other", "different", None)
    facts = ms.facts_for_project(c, "proj")
    assert [f["text"] for f in facts] == ["uses MSVC", "tabs not spaces"]
    assert ms.count_facts(c, "proj") == 2
    assert ms.count_facts(c, "other") == 1
