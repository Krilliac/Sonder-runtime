"""Task/checklist adapters (SPEC-5 WP11, relocated from legacy)."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Mapping

from ..domain.common.errors import DependencyUnavailable, InvalidInput, NotFound


def _store_call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except ValueError as exc:
        if str(exc).startswith("no unique task '"):
            raise NotFound(str(exc)) from exc
        raise InvalidInput(str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise DependencyUnavailable(str(exc)) from exc


class LegacyTaskRepository:
    def __init__(self, connection) -> None:
        self._connection = connection

    def create(self, *, account_scope: str | None = None, **fields) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.create_task,
            self._connection,
            account_scope=account_scope,
            **fields,
        )

    def list(self, *, account_scope: str | None = None, **filters) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.list_tasks,
            self._connection,
            account_scope=account_scope,
            **filters,
        )

    def update(
        self, task_id: str, *, account_scope: str | None = None, **changes
    ) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.update_task,
            self._connection,
            task_id,
            account_scope=account_scope,
            **changes,
        )

    def get(self, task_id: str, *, account_scope: str | None = None) -> dict | None:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.get_task,
            self._connection,
            task_id,
            account_scope=account_scope,
        )

    def events(
        self, task_id: str, limit: int = 20, *, account_scope: str | None = None
    ) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.task_events,
            self._connection,
            task_id,
            limit=limit,
            account_scope=account_scope,
        )

    def children(self, task_id: str, *, account_scope: str | None = None) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.task_children,
            self._connection,
            task_id,
            account_scope=account_scope,
        )

    def delete(self, task_id: str, *, account_scope: str | None = None) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.delete_task,
            self._connection,
            task_id,
            account_scope=account_scope,
        )

    def add_dependency(
        self, task_id: str, depends_on: str, *, account_scope: str | None = None
    ) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.add_task_dep,
            self._connection,
            task_id,
            depends_on,
            account_scope=account_scope,
        )

    def remove_dependency(
        self, task_id: str, depends_on: str, *, account_scope: str | None = None
    ) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.remove_task_dep,
            self._connection,
            task_id,
            depends_on,
            account_scope=account_scope,
        )

    def dependencies(
        self, task_id: str, *, account_scope: str | None = None
    ) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.task_dependencies,
            self._connection,
            task_id,
            account_scope=account_scope,
        )

    def dependents(
        self, task_id: str, *, account_scope: str | None = None
    ) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.task_dependents,
            self._connection,
            task_id,
            account_scope=account_scope,
        )

    def progress(self, project: str = "", *, account_scope: str | None = None) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.task_progress,
            self._connection,
            project=project,
            account_scope=account_scope,
        )


class LegacyChecklistEventSink:
    def __init__(self, publish_fn: Callable[[dict], None]) -> None:
        self._publish_fn = publish_fn

    def publish(self, checklist: Mapping[str, object]) -> None:
        self._publish_fn(dict(checklist))
