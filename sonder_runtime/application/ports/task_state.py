"""Narrow ports for visible task and checklist state."""
from __future__ import annotations

from typing import Mapping, Protocol


class TaskRepository(Protocol):
    def create(self, **fields) -> dict: ...

    def list(self, **filters) -> list[dict]: ...

    def update(self, task_id: str, **changes) -> dict: ...

    def get(self, task_id: str) -> dict | None: ...

    def events(self, task_id: str, limit: int = 20) -> list[dict]: ...

    def children(self, task_id: str) -> list[dict]: ...


class ChecklistEventPort(Protocol):
    """Publish the latest checklist projection to operator-visible activity."""

    def publish(self, checklist: Mapping[str, object]) -> None: ...
