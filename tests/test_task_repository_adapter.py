from __future__ import annotations

import sqlite3

import pytest

import sonder_runtime.adapters.memory_store as memory_store
from sonder_runtime.adapters.task_repository import TaskRepositoryAdapter
from sonder_runtime.adapters.task_store import LegacyTaskRepository
from sonder_runtime.domain.common.errors import DependencyUnavailable, InvalidInput, NotFound


def test_legacy_task_repository_is_identity_compatible_with_canonical_adapter():
    assert LegacyTaskRepository is TaskRepositoryAdapter


def test_task_repository_delegates_to_memory_store(monkeypatch):
    calls = []

    def create_task(connection, **kwargs):
        calls.append((connection, kwargs))
        return {"id": "task-1"}

    monkeypatch.setattr(memory_store, "create_task", create_task)

    result = TaskRepositoryAdapter("connection").create(
        account_scope="acct", title="migration"
    )

    assert result == {"id": "task-1"}
    assert calls == [("connection", {"account_scope": "acct", "title": "migration"})]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("no unique task 'x'"), NotFound),
        (ValueError("invalid status"), InvalidInput),
        (sqlite3.Error("database unavailable"), DependencyUnavailable),
    ],
)
def test_task_repository_maps_store_failures(error, expected, monkeypatch):
    def create_task(*args, **kwargs):
        raise error

    monkeypatch.setattr(memory_store, "create_task", create_task)

    with pytest.raises(expected):
        TaskRepositoryAdapter("connection").create(title="migration")
