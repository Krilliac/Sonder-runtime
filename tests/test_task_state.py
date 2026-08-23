import memory_store
import server
import sqlite3

import pytest


def test_task_create_list_update_and_events():
    conn = memory_store.connect(":memory:")
    task = memory_store.create_task(
        conn,
        "port useful workflow controls",
        detail="visible task state",
        priority=1,
        project="sonder",
    )

    assert task["status"] == "pending"
    assert task["priority"] == 1
    assert memory_store.list_tasks(conn, project="sonder")[0]["id"] == task["id"]

    updated = memory_store.update_task(
        conn,
        task["id"][:8],
        status="doing",
        note="started implementation",
    )
    assert updated["status"] == "in_progress"

    events = memory_store.task_events(conn, task["id"])
    assert [event["event"] for event in events] == ["updated", "created"]


def test_task_dependency_rejects_direct_and_transitive_cycles():
    conn = memory_store.connect(":memory:")
    first = memory_store.create_task(conn, "design")
    second = memory_store.create_task(conn, "implement")
    third = memory_store.create_task(conn, "validate")
    memory_store.add_task_dep(conn, second["id"], first["id"])
    memory_store.add_task_dep(conn, third["id"], second["id"])

    with pytest.raises(ValueError, match="cannot depend on itself"):
        memory_store.add_task_dep(conn, first["id"], first["id"])
    with pytest.raises(ValueError, match="would create a cycle"):
        memory_store.add_task_dep(conn, first["id"], second["id"])
    with pytest.raises(ValueError, match="would create a cycle"):
        memory_store.add_task_dep(conn, first["id"], third["id"])
    # The reverse-direction edge that does not close a loop is still legal.
    result = memory_store.add_task_dep(conn, third["id"], first["id"])
    assert result["depends_on"] == first["id"]


def test_task_list_hides_done_by_default():
    conn = memory_store.connect(":memory:")
    done = memory_store.create_task(conn, "finished", status="done")
    memory_store.create_task(conn, "open")

    rows = memory_store.list_tasks(conn)
    assert [row["title"] for row in rows] == ["open"]
    assert done["id"] in [row["id"] for row in memory_store.list_tasks(conn, include_done=True)]


def test_task_list_accepts_pipe_delimited_status_filter():
    conn = memory_store.connect(":memory:")
    pending = memory_store.create_task(conn, "pending", status="pending")
    blocked = memory_store.create_task(conn, "blocked", status="blocked")
    memory_store.create_task(conn, "done", status="done")

    rows = memory_store.list_tasks(conn, status="pending|blocked")

    assert {row["id"] for row in rows} == {pending["id"], blocked["id"]}


def test_task_list_accepts_typed_multi_status_filter_without_stringifying_it():
    conn = memory_store.connect(":memory:")
    pending = memory_store.create_task(conn, "pending", status="pending")
    blocked = memory_store.create_task(conn, "blocked", status="blocked")
    memory_store.create_task(conn, "done", status="done")

    rows = memory_store.list_tasks(conn, status=["pending", "blocked", "pending"])

    assert {row["id"] for row in rows} == {pending["id"], blocked["id"]}


@pytest.mark.parametrize("value", [{"status": "pending"}, ["pending", 1]])
def test_task_list_rejects_non_string_status_filter_entries(value):
    conn = memory_store.connect(":memory:")

    with pytest.raises(ValueError, match="task status filter"):
        memory_store.list_tasks(conn, status=value)


def test_task_list_contract_documents_multi_status_filtering():
    assert "pipe-delimited set" in server.task_list.__doc__
    assert "pending|blocked" in server.task_list.__doc__
    assert "typed JSON array" in server.task_list.__doc__


def test_account_scoped_task_operations_are_isolated_but_local_default_is_global():
    conn = memory_store.connect(":memory:")
    alpha_parent = memory_store.create_task(
        conn, "alpha plan", task_id="alpha-parent", account_scope="account:alpha"
    )
    alpha_child = memory_store.create_task(
        conn,
        "alpha child",
        parent_id=alpha_parent["id"],
        task_id="alpha-child",
        account_scope="account:alpha",
    )
    beta = memory_store.create_task(
        conn, "beta task", task_id="beta-task", account_scope="account:beta"
    )
    legacy = memory_store.create_task(conn, "local task", task_id="local-task")

    assert [row["id"] for row in memory_store.list_tasks(
        conn, account_scope="account:alpha"
    )] == [alpha_child["id"], alpha_parent["id"]]
    assert memory_store.get_task(
        conn, beta["id"], account_scope="account:alpha"
    ) is None
    assert memory_store.task_events(
        conn, beta["id"], account_scope="account:alpha"
    ) == []
    assert [row["id"] for row in memory_store.task_children(
        conn, alpha_parent["id"], account_scope="account:alpha"
    )] == [alpha_child["id"]]
    assert memory_store.task_progress(conn, account_scope="account:alpha")["total"] == 2

    with pytest.raises(ValueError, match="no unique task 'beta-task'"):
        memory_store.update_task(
            conn, beta["id"], status="done", account_scope="account:alpha"
        )
    with pytest.raises(ValueError, match="no unique task 'beta-task'"):
        memory_store.add_task_dep(
            conn, alpha_child["id"], beta["id"], account_scope="account:alpha"
        )
    dependency = memory_store.add_task_dep(
        conn, alpha_child["id"], alpha_parent["id"], account_scope="account:alpha"
    )
    assert dependency["depends_on"] == alpha_parent["id"]
    assert [row["id"] for row in memory_store.task_dependencies(
        conn, alpha_child["id"], account_scope="account:alpha"
    )] == [alpha_parent["id"]]
    assert [row["id"] for row in memory_store.task_dependents(
        conn, alpha_parent["id"], account_scope="account:alpha"
    )] == [alpha_child["id"]]
    assert memory_store.remove_task_dep(
        conn, alpha_child["id"], alpha_parent["id"], account_scope="account:alpha"
    )["removed"] is True
    with pytest.raises(ValueError, match="no unique task 'beta-task'"):
        memory_store.delete_task(conn, beta["id"], account_scope="account:alpha")

    disposable = memory_store.create_task(
        conn, "alpha disposable", account_scope="account:alpha"
    )
    assert memory_store.delete_task(
        conn, disposable["id"], account_scope="account:alpha"
    )["deleted"] == disposable["id"]

    # Calls without a scope retain local-single-user behavior and can see the
    # old unscoped rows as well as all scoped rows.
    assert {row["id"] for row in memory_store.list_tasks(conn)} == {
        alpha_parent["id"], alpha_child["id"], beta["id"], legacy["id"],
    }


def test_task_account_scope_migrates_without_assigning_legacy_ownership(tmp_path):
    path = tmp_path / "legacy-tasks.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE tasks ("
            "id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT, "
            "status TEXT DEFAULT 'pending', priority INTEGER DEFAULT 2, "
            "project TEXT, owner TEXT, parent_id TEXT, "
            "created_ts TEXT DEFAULT CURRENT_TIMESTAMP, "
            "updated_ts TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO tasks(id, title) VALUES('legacy-id', 'legacy')")
        conn.commit()
        memory_store.init_db(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert "account_scope" in columns
        assert conn.execute(
            "SELECT account_scope FROM tasks WHERE id='legacy-id'"
        ).fetchone()[0] is None
        assert memory_store.get_task(conn, "legacy-id")["title"] == "legacy"
        assert memory_store.get_task(
            conn, "legacy-id", account_scope="account:alpha"
        ) is None
        index_columns = [row[2] for row in conn.execute(
            "PRAGMA index_info(idx_tasks_status_project)"
        )]
        assert index_columns[:2] == ["account_scope", "status"]
    finally:
        conn.close()
