"""Pure, bounded task-ledger projections for manager-style orchestration."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import hashlib
import json

from .common.errors import InvalidInput


MAX_LEDGER_ITEMS = 256
_MAX_TEXT = 4096
COMPLETED_TASK_STATUSES = frozenset({"done", "completed", "succeeded"})
RUNNABLE_TASK_STATUSES = frozenset({"pending", "ready", "queued"})


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
        by_id = {item.task_id: item for item in self.items}
        remaining = {task_id: len(item.dependencies) for task_id, item in by_id.items()}
        dependents: dict[str, list[str]] = {task_id: [] for task_id in by_id}
        for item in self.items:
            for dependency in item.dependencies:
                dependents[dependency].append(item.task_id)
        ready = sorted(task_id for task_id, count in remaining.items() if count == 0)
        visited = 0
        while ready:
            task_id = ready.pop(0)
            visited += 1
            for dependent in sorted(dependents[task_id]):
                remaining[dependent] -= 1
                if remaining[dependent] == 0:
                    ready.append(dependent)
            ready.sort()
        if visited != len(by_id):
            cycle = sorted(task_id for task_id, count in remaining.items() if count > 0)
            raise InvalidInput("task ledger dependency cycle: " + " -> ".join(cycle))
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

    def ready_items(
        self,
        *,
        max_fanout: int = 8,
        runnable_statuses: frozenset[str] = RUNNABLE_TASK_STATUSES,
        completed_statuses: frozenset[str] = COMPLETED_TASK_STATUSES,
    ) -> tuple[TaskLedgerItem, ...]:
        """Return a deterministic, bounded dependency-ready dispatch batch."""
        if isinstance(max_fanout, bool) or not isinstance(max_fanout, int) or max_fanout < 1:
            raise InvalidInput("max_fanout must be a positive integer")
        by_id = {item.task_id: item for item in self.items}
        ready = (
            item for item in self.items
            if item.status in runnable_statuses
            and all(by_id[dependency].status in completed_statuses for dependency in item.dependencies)
        )
        return tuple(sorted(ready, key=lambda item: item.task_id))[:max_fanout]

    def blocked_dependencies(
        self,
        *,
        runnable_statuses: frozenset[str] = RUNNABLE_TASK_STATUSES,
        completed_statuses: frozenset[str] = COMPLETED_TASK_STATUSES,
    ) -> dict[str, tuple[str, ...]]:
        """Explain why runnable tasks are not dependency-ready."""
        by_id = {item.task_id: item for item in self.items}
        return {
            item.task_id: tuple(
                dependency for dependency in item.dependencies
                if by_id[dependency].status not in completed_statuses
            )
            for item in self.items
            if item.status in runnable_statuses
            and any(by_id[dependency].status not in completed_statuses for dependency in item.dependencies)
        }

    def dependency_batches(self, *, max_fanout: int = 8) -> tuple[tuple[str, ...], ...]:
        """Project a stable topological schedule split into bounded batches."""
        if isinstance(max_fanout, bool) or not isinstance(max_fanout, int) or max_fanout < 1:
            raise InvalidInput("max_fanout must be a positive integer")
        remaining = {item.task_id: set(item.dependencies) for item in self.items}
        batches: list[tuple[str, ...]] = []
        while remaining:
            level = sorted(task_id for task_id, dependencies in remaining.items() if not dependencies)
            # Cycles are rejected during construction, so this is defensive only.
            if not level:
                raise InvalidInput("task ledger dependency cycle")
            for offset in range(0, len(level), max_fanout):
                batches.append(tuple(level[offset:offset + max_fanout]))
            completed = set(level)
            remaining = {
                task_id: dependencies - completed
                for task_id, dependencies in remaining.items()
                if task_id not in completed
            }
        return tuple(batches)


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


__all__ = [
    "COMPLETED_TASK_STATUSES", "MAX_LEDGER_ITEMS", "RUNNABLE_TASK_STATUSES",
    "TaskLedger", "TaskLedgerItem", "build_task_ledger",
]
