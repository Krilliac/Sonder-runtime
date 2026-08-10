"""Typed use cases for visible tasks and ordered checklists.

The legacy store commits every ``create`` and ``update`` independently.  The
service deliberately preserves that contract: checklist input is fully
validated before the first write, but a later repository failure leaves the
parent and any earlier children durable.  Tightening this to an atomic unit is
a separate schema/transaction migration, not a side effect of extraction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from ...domain.common.errors import InvalidInput, NotFound
from ..ports.task_state import ChecklistEventPort, TaskRepository


def normalize_checklist_items(items_json) -> list[tuple[str, str]]:
    """Validate the complete checklist before any repository write occurs."""
    try:
        items = json.loads(items_json) if isinstance(items_json, str) else items_json
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidInput(str(exc)) from exc
    if not isinstance(items, list) or not items:
        raise InvalidInput("items_json must be a non-empty JSON list")
    if len(items) > 20:
        raise InvalidInput("a checklist supports at most 20 items")
    normalized = []
    for item in items:
        if isinstance(item, dict):
            item_title = str(item.get("title", "")).strip()
            detail = str(item.get("detail", ""))
        else:
            item_title = str(item).strip()
            detail = ""
        if not item_title:
            raise InvalidInput("checklist item titles cannot be empty")
        normalized.append((item_title, detail))
    return normalized


@dataclass(frozen=True)
class TaskView:
    id: str
    title: str
    detail: str
    status: str
    priority: int
    project: str
    owner: str
    parent_id: str

    @classmethod
    def from_raw(cls, raw: dict) -> "TaskView":
        return cls(
            id=raw.get("id", ""), title=raw.get("title", ""),
            detail=raw.get("detail", ""), status=raw.get("status", "pending"),
            priority=raw.get("priority", 2), project=raw.get("project", ""),
            owner=raw.get("owner", ""), parent_id=raw.get("parent_id", ""),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "detail": self.detail,
            "status": self.status, "priority": self.priority,
            "project": self.project, "owner": self.owner,
            "parent_id": self.parent_id,
        }


@dataclass(frozen=True)
class TaskDetail:
    task: TaskView | None
    events: tuple[dict, ...]


@dataclass(frozen=True)
class ChecklistView:
    id: str
    title: str
    status: str
    project: str
    owner: str
    items: tuple[TaskView, ...]

    @property
    def summary(self) -> str:
        done = sum(1 for item in self.items if item.status == "done")
        return "%d/%d complete" % (done, len(self.items))

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "status": self.status,
            "project": self.project, "owner": self.owner,
            "items": [item.to_dict() for item in self.items],
            "summary": self.summary,
        }


class TaskService:
    def __init__(
        self, repository: TaskRepository, checklist_events: ChecklistEventPort
    ) -> None:
        self._repository = repository
        self._checklist_events = checklist_events

    def create_task(self, **fields) -> TaskView:
        return TaskView.from_raw(self._repository.create(**fields))

    def list_tasks(self, **filters) -> tuple[TaskView, ...]:
        return tuple(TaskView.from_raw(row) for row in self._repository.list(**filters))

    def update_task(self, task_id: str, **changes) -> TaskView:
        return TaskView.from_raw(self._repository.update(task_id, **changes))

    def show_task(self, task_id: str, *, include_events: bool = True) -> TaskDetail:
        raw = self._repository.get(task_id)
        events = self._repository.events(task_id, limit=20) if include_events else []
        return TaskDetail(TaskView.from_raw(raw) if raw else None, tuple(events))

    def checklist(self, checklist_id: str) -> ChecklistView:
        parent = self._repository.get(checklist_id)
        if not parent:
            raise NotFound("no checklist '%s'" % checklist_id)
        items = tuple(
            TaskView.from_raw(row) for row in self._repository.children(parent["id"])
        )
        return ChecklistView(
            id=parent["id"], title=parent.get("title", ""),
            status=parent.get("status", "pending"),
            project=parent.get("project", ""), owner=parent.get("owner", ""),
            items=items,
        )

    def create_checklist(
        self, title: str, items_json, *, project: str = "",
        owner: str = "agent", priority: int = 1,
    ) -> ChecklistView:
        normalized = normalize_checklist_items(items_json)
        parent = self._repository.create(
            title=title, detail="work checklist", status="in_progress",
            priority=priority, project=project, owner=owner,
        )
        for item_title, detail in normalized:
            self._repository.create(
                title=item_title, detail=detail, status="pending",
                priority=priority, project=project, owner=owner,
                parent_id=parent["id"],
            )
        return self.checklist(parent["id"])

    def update_checklist(
        self, checklist_id: str, item: str, status: str, note: str = ""
    ) -> ChecklistView:
        data = self.checklist(checklist_id)
        selected = None
        value = str(item or "").strip()
        if value.isdigit() and 1 <= int(value) <= len(data.items):
            selected = data.items[int(value) - 1]
        else:
            matches = [row for row in data.items if row.id.startswith(value)]
            if len(matches) == 1:
                selected = matches[0]
        if not selected:
            raise NotFound("no unique checklist item '%s'" % item)
        self._repository.update(
            selected.id, status=status, note=note or "checklist update"
        )
        data = self.checklist(checklist_id)
        states = [row.status for row in data.items]
        parent_status = (
            "done" if states and all(state in ("done", "canceled") for state in states)
            else "blocked" if "blocked" in states else "in_progress"
        )
        self._repository.update(
            data.id, status=parent_status, note="checklist %s" % data.summary
        )
        return self.checklist(checklist_id)

    def delete_task(self, task_id: str) -> dict:
        return self._repository.delete(task_id)

    def add_dependency(self, task_id: str, depends_on: str) -> dict:
        return self._repository.add_dependency(task_id, depends_on)

    def remove_dependency(self, task_id: str, depends_on: str) -> dict:
        return self._repository.remove_dependency(task_id, depends_on)

    def task_dependencies(self, task_id: str) -> list[TaskView]:
        return [TaskView.from_raw(r) for r in self._repository.dependencies(task_id)]

    def task_dependents(self, task_id: str) -> list[TaskView]:
        return [TaskView.from_raw(r) for r in self._repository.dependents(task_id)]

    def task_progress(self, project: str = "") -> dict:
        return self._repository.progress(project=project)

    def plan_tasks(
        self,
        title: str,
        steps: list[str | dict],
        *,
        project: str = "",
        owner: str = "agent",
        priority: int = 2,
        sequential: bool = True,
    ) -> ChecklistView:
        if not steps:
            raise InvalidInput("at least one step is required")
        if len(steps) > 30:
            raise InvalidInput("a plan supports at most 30 steps")
        parent = self._repository.create(
            title=title, detail="work plan", status="in_progress",
            priority=priority, project=project, owner=owner,
        )
        child_ids = []
        for step in steps:
            if isinstance(step, dict):
                step_title = str(step.get("title", "")).strip()
                detail = str(step.get("detail", ""))
            else:
                step_title = str(step).strip()
                detail = ""
            if not step_title:
                raise InvalidInput("plan step titles cannot be empty")
            child = self._repository.create(
                title=step_title, detail=detail, status="pending",
                priority=priority, project=project, owner=owner,
                parent_id=parent["id"],
            )
            child_ids.append(child["id"])
        if sequential and len(child_ids) > 1:
            for i in range(1, len(child_ids)):
                self._repository.add_dependency(child_ids[i], child_ids[i - 1])
        return self.checklist(parent["id"])

    def publish_checklist(self, checklist: ChecklistView) -> None:
        self._checklist_events.publish(checklist.to_dict())
