"""Legacy SQLite and activity adapters for task/checklist use cases."""
from __future__ import annotations

from collections.abc import Callable
from typing import Mapping


class LegacyTaskRepository:
    def __init__(self, connection) -> None:
        self._connection = connection

    def create(self, **fields) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return memory_store.create_task(self._connection, **fields)

    def list(self, **filters) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return memory_store.list_tasks(self._connection, **filters)

    def update(self, task_id: str, **changes) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return memory_store.update_task(self._connection, task_id, **changes)

    def get(self, task_id: str) -> dict | None:
        import sonder_runtime.adapters.memory_store as memory_store
        return memory_store.get_task(self._connection, task_id)

    def events(self, task_id: str, limit: int = 20) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return memory_store.task_events(self._connection, task_id, limit=limit)

    def children(self, task_id: str) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return memory_store.task_children(self._connection, task_id)


class LegacyChecklistEventSink:
    def __init__(self, publish_fn: Callable[[dict], None]) -> None:
        self._publish_fn = publish_fn

    def publish(self, checklist: Mapping[str, object]) -> None:
        self._publish_fn(dict(checklist))
