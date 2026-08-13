"""Narrow ports for visible task and checklist state."""
from __future__ import annotations

from typing import Mapping, Protocol


class TaskRepository(Protocol):
    def create(self, *, account_scope: str | None = None, **fields) -> dict: ...

    def list(self, *, account_scope: str | None = None, **filters) -> list[dict]: ...

    def update(
        self, task_id: str, *, account_scope: str | None = None, **changes
    ) -> dict: ...

    def get(self, task_id: str, *, account_scope: str | None = None) -> dict | None: ...

    def events(
        self, task_id: str, limit: int = 20, *, account_scope: str | None = None
    ) -> list[dict]: ...

    def children(self, task_id: str, *, account_scope: str | None = None) -> list[dict]: ...

    def delete(self, task_id: str, *, account_scope: str | None = None) -> dict: ...

    def add_dependency(
        self, task_id: str, depends_on: str, *, account_scope: str | None = None
    ) -> dict: ...

    def remove_dependency(
        self, task_id: str, depends_on: str, *, account_scope: str | None = None
    ) -> dict: ...

    def dependencies(
        self, task_id: str, *, account_scope: str | None = None
    ) -> list[dict]: ...

    def dependents(
        self, task_id: str, *, account_scope: str | None = None
    ) -> list[dict]: ...

    def progress(
        self, project: str = "", *, account_scope: str | None = None
    ) -> dict: ...


class ChecklistEventPort(Protocol):
    """Publish the latest checklist projection to operator-visible activity."""

    def publish(self, checklist: Mapping[str, object]) -> None: ...
