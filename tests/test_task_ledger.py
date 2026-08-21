from __future__ import annotations

import pytest

from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.task_ledger import TaskLedger, build_task_ledger
from sonder_runtime.adapters import memory_store
from sonder_runtime.adapters.task_store import LegacyTaskRepository
from sonder_runtime.application.tasks.use_cases import TaskService


def _tasks():
    return [
        {"id": "b", "title": "Validate", "status": "pending", "owner": "reviewer"},
        {"id": "a", "title": "Research", "status": "done", "owner": "researcher"},
    ]


def test_task_ledger_is_sorted_and_digest_bound():
    ledger = build_task_ledger("goal-1", _tasks(), {"b": ("a",)}, replan_count=2, last_replan_reason="new evidence")
    assert [item.task_id for item in ledger.items] == ["a", "b"]
    assert ledger.items[1].dependencies == ("a",)
    assert ledger.to_dict()["schema"] == "sonder.task-ledger.v1"
    assert ledger.digest() == TaskLedger("goal-1", ledger.items, 2, "new evidence").digest()


def test_task_ledger_rejects_missing_dependencies_and_self_edges():
    with pytest.raises(InvalidInput, match="missing dependency"):
        build_task_ledger("goal", _tasks(), {"b": ("missing",)})
    with pytest.raises(InvalidInput, match="itself"):
        build_task_ledger("goal", _tasks(), {"b": ("b",)})


def test_task_ledger_rejects_duplicate_items_and_invalid_replan_count():
    with pytest.raises(InvalidInput, match="unique"):
        build_task_ledger("goal", _tasks() + [_tasks()[0]])
    with pytest.raises(InvalidInput, match="replan_count"):
        build_task_ledger("goal", _tasks(), replan_count=-1)


def test_task_service_exposes_repository_backed_ledger(tmp_path):
    connection = memory_store.connect(str(tmp_path / "ledger.db"))
    try:
        service = TaskService(LegacyTaskRepository(connection), lambda: None)
        plan = service.plan_tasks("goal", ["research", "validate"], project="sonder")
        ledger = service.task_ledger(plan.id, replan_count=1, last_replan_reason="new evidence")
        assert ledger.goal_id == plan.id
        by_title = {item.title: item for item in ledger.items}
        assert by_title["research"].dependencies == ()
        assert by_title["validate"].dependencies == (by_title["research"].task_id,)
        assert ledger.replan_count == 1
    finally:
        connection.close()
