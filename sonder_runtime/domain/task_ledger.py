"""Pure, bounded task-ledger projections for manager-style orchestration."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json

from .common.errors import InvalidInput


MAX_LEDGER_ITEMS = 256
_MAX_TEXT = 4096


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise InvalidInput(f"{label} must be bounded non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class TaskLedgerItem:
    task_id: str
    title: str
    status: str
    owner: str = ""
    parent_id: str = ""
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.task_id, "task_id")
        _text(self.title, "title")
        _text(self.status, "status")
        if self.owner:
            _text(self.owner, "owner")
        if self.parent_id:
            _text(self.parent_id, "parent_id")
        dependencies = tuple(sorted(set(self.dependencies)))
        if len(dependencies) != len(self.dependencies):
            raise InvalidInput("task dependencies must be unique")
        for dependency in dependencies:
            _text(dependency, "dependency")
        if self.task_id in dependencies:
            raise InvalidInput("task cannot depend on itself")
        object.__setattr__(self, "dependencies", dependencies)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "owner": self.owner,
            "parent_id": self.parent_id,
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True, slots=True)
class TaskLedger:
    goal_id: str
    items: tuple[TaskLedgerItem, ...]
    replan_count: int = 0
    last_replan_reason: str | None = None

    def __post_init__(self) -> None:
        _text(self.goal_id, "goal_id")
        if not self.items or len(self.items) > MAX_LEDGER_ITEMS:
            raise InvalidInput("task ledger must contain 1..256 items")
        ids = {item.task_id for item in self.items}
        if len(ids) != len(self.items):
            raise InvalidInput("task ledger task IDs must be unique")
        missing = sorted({dependency for item in self.items for dependency in item.dependencies} - ids)
        if missing:
            raise InvalidInput("task ledger references missing dependency: " + missing[0])
        if isinstance(self.replan_count, bool) or not isinstance(self.replan_count, int) or self.replan_count < 0:
            raise InvalidInput("replan_count must be a non-negative integer")
        if self.last_replan_reason is not None:
            _text(self.last_replan_reason, "last_replan_reason")
        object.__setattr__(self, "items", tuple(sorted(self.items, key=lambda item: item.task_id)))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "sonder.task-ledger.v1",
            "goal_id": self.goal_id,
            "items": [item.to_dict() for item in self.items],
            "replan_count": self.replan_count,
            "last_replan_reason": self.last_replan_reason,
        }

    def digest(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def build_task_ledger(
    goal_id: str,
    tasks: Iterable[Mapping[str, object]],
    dependencies: Mapping[str, Iterable[str]] | None = None,
    *,
    replan_count: int = 0,
    last_replan_reason: str | None = None,
) -> TaskLedger:
    """Build a deterministic projection without reading or mutating storage."""
    dependency_map = dependencies or {}
    items = tuple(
        TaskLedgerItem(
            task_id=task.get("id", ""), title=task.get("title", ""),
            status=task.get("status", "pending"), owner=task.get("owner", ""),
            parent_id=task.get("parent_id", ""),
            dependencies=tuple(dependency_map.get(task.get("id", ""), ())),
        )
        for task in tasks
    )
    return TaskLedger(goal_id, items, replan_count, last_replan_reason)


__all__ = ["MAX_LEDGER_ITEMS", "TaskLedger", "TaskLedgerItem", "build_task_ledger"]
