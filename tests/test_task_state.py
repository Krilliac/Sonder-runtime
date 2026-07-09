import memory_store


def test_task_create_list_update_and_events():
    conn = memory_store.connect(":memory:")
    task = memory_store.create_task(
        conn,
        "port useful workflow controls",
        detail="visible task state",
        priority=1,
        project="trilobite",
    )

    assert task["status"] == "pending"
    assert task["priority"] == 1
    assert memory_store.list_tasks(conn, project="trilobite")[0]["id"] == task["id"]

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
