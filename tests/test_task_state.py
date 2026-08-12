import memory_store
import server


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


def test_task_list_contract_documents_multi_status_filtering():
    assert "pipe-delimited set" in server.task_list.__doc__
    assert "pending|blocked" in server.task_list.__doc__
