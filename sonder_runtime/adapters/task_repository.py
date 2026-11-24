"""Canonical task/checklist persistence adapter."""
from __future__ import annotations

from .repository_errors import call_repository_operation


# Keep the historical private helper available to local compatibility tests
# while making error-translation ownership explicit in its own adapter.
_store_call = call_repository_operation


class TaskRepositoryAdapter:
    """Translate the task persistence port to the memory-store boundary."""

    def __init__(self, connection, *, default_account_scope: str | None = None) -> None:
        self._connection = connection
        if default_account_scope is None:
            self._default_account_scope: str | None = None
        else:
            if not isinstance(default_account_scope, str):
                raise ValueError("task account scope must be a string")
            cleaned = default_account_scope.strip()
            if not cleaned:
                raise ValueError("task account scope must not be empty")
            if len(cleaned) > 256:
                raise ValueError("task account scope is too long")
            self._default_account_scope = cleaned

    def _resolve_scope(self, account_scope: str | None) -> str | None:
        """Keep an active repository default scope unbreakable and non-widening."""
        if self._default_account_scope is not None:
            if account_scope is not None and account_scope != self._default_account_scope:
                raise PermissionError("task account scope mismatch")
            return self._default_account_scope
        return account_scope

    def create(self, *, account_scope: str | None = None, **fields) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.create_task,
            self._connection,
            account_scope=self._resolve_scope(account_scope),
            **fields,
        )

    def list(self, *, account_scope: str | None = None, **filters) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.list_tasks,
            self._connection,
            account_scope=self._resolve_scope(account_scope),
            **filters,
        )

    def latest_checklist(self, *, account_scope: str | None = None) -> dict | None:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.latest_checklist,
            self._connection,
            account_scope=self._resolve_scope(account_scope),
        )

    def update(
        self, task_id: str, *, account_scope: str | None = None, **changes
    ) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.update_task,
            self._connection,
            task_id,
            account_scope=self._resolve_scope(account_scope),
            **changes,
        )

    def get(self, task_id: str, *, account_scope: str | None = None) -> dict | None:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.get_task,
            self._connection,
            task_id,
            account_scope=self._resolve_scope(account_scope),
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
            account_scope=self._resolve_scope(account_scope),
        )

    def children(self, task_id: str, *, account_scope: str | None = None) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.task_children,
            self._connection,
            task_id,
            account_scope=self._resolve_scope(account_scope),
        )

    def delete(self, task_id: str, *, account_scope: str | None = None) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.delete_task,
            self._connection,
            task_id,
            account_scope=self._resolve_scope(account_scope),
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
            account_scope=self._resolve_scope(account_scope),
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
            account_scope=self._resolve_scope(account_scope),
        )

    def dependencies(
        self, task_id: str, *, account_scope: str | None = None
    ) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.task_dependencies,
            self._connection,
            task_id,
            account_scope=self._resolve_scope(account_scope),
        )

    def dependents(
        self, task_id: str, *, account_scope: str | None = None
    ) -> list[dict]:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.task_dependents,
            self._connection,
            task_id,
            account_scope=self._resolve_scope(account_scope),
        )

    def progress(self, project: str = "", *, account_scope: str | None = None) -> dict:
        import sonder_runtime.adapters.memory_store as memory_store
        return _store_call(
            memory_store.task_progress,
            self._connection,
            project=project,
            account_scope=self._resolve_scope(account_scope),
        )


# Compatibility name retained for callers using the pre-canonical adapter.
LegacyTaskRepository = TaskRepositoryAdapter
