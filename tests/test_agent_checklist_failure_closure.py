"""An agent that exits early closes every checklist row it left open (#43).

The failure path blocked only the failing step and closed the report step,
so a retried workbench run left "Inspect relevant folders..." in_progress
and "Validate results..." pending forever under a blocked parent, which read
in /tasks like live work.
"""
import pytest

import server


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_APP_GRAPH", None)


def _statuses(checklist_id):
    conn = server._open_db()
    try:
        checklist = server._task_service(conn).checklist(checklist_id)
    finally:
        conn.close()
    return checklist.status, [item.status for item in checklist.items]


@pytest.mark.parametrize("failed_item", [1, 2, 3])
def test_early_exit_closes_open_rows(isolated_db, failed_item):
    checklist_id, states = server._start_agent_checklist("fix the parser", "", False)
    assert checklist_id

    server._agent_checklist_fail(checklist_id, states, "model call timed out", item=failed_item)

    parent, items = _statuses(checklist_id)
    assert parent == "blocked"
    assert items[failed_item - 1] == "blocked"
    assert items[3] == "done"
    assert "in_progress" not in items and "pending" not in items
    for index, status in enumerate(items[:3], start=1):
        if index != failed_item:
            assert status == "canceled"


def test_rows_already_done_keep_their_status(isolated_db):
    checklist_id, states = server._start_agent_checklist("fix the parser", "", False)
    server._agent_checklist_mark(checklist_id, states, 1, "done", "inspected")

    server._agent_checklist_fail(checklist_id, states, "validation crashed", item=3)

    _parent, items = _statuses(checklist_id)
    assert items == ["done", "canceled", "blocked", "done"]
