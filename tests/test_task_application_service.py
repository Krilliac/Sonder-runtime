"""Typed application-service tests for task/checklist behavior."""
import pytest

from sonder_runtime.adapters import memory_store
from sonder_runtime.adapters.task_store import LegacyTaskRepository
from sonder_runtime.application.tasks.use_cases import TaskService
from sonder_runtime.domain.common.errors import (
    DependencyUnavailable,
    InvalidInput,
    NotFound,
)


class RecordingRepository:
    def __init__(self, fail_create_at=None):
        self.rows = []
        self.fail_create_at = fail_create_at
        self.create_calls = 0

    def create(self, **fields):
        self.create_calls += 1
        if self.create_calls == self.fail_create_at:
            raise RuntimeError("injected create failure")
        row = {"id": "id-%d" % self.create_calls, "priority": 2,
               "status": "pending", "detail": "", "project": "",
               "owner": "", "parent_id": "", **fields}
        self.rows.append(row)
        return row

    def list(self, **filters):
        return list(self.rows)

    def update(self, task_id, **changes):
        row = next(row for row in self.rows if row["id"] == task_id)
        row.update({key: value for key, value in changes.items() if key != "note"})
        return row

    def get(self, task_id):
        return next((row for row in self.rows if row["id"] == task_id), None)

    def events(self, task_id, limit=20):
        return []

    def children(self, task_id):
        return [row for row in self.rows if row.get("parent_id") == task_id]


class RecordingEvents:
    def __init__(self):
        self.rows = []

    def publish(self, checklist):
        self.rows.append(dict(checklist))


def test_checklist_validates_every_item_before_first_write():
    repository = RecordingRepository()
    service = TaskService(repository, RecordingEvents())
    with pytest.raises(InvalidInput, match="titles cannot be empty"):
        service.create_checklist("invalid", ["valid", ""])
    assert repository.create_calls == 0
    assert repository.rows == []


def test_checklist_documents_legacy_partial_persistence_on_late_failure():
    repository = RecordingRepository(fail_create_at=3)
    service = TaskService(repository, RecordingEvents())
    with pytest.raises(RuntimeError, match="injected create failure"):
        service.create_checklist("partial", ["first", "second"])
    assert [row["title"] for row in repository.rows] == ["partial", "first"]


def test_checklist_publication_is_explicit_and_typed():
    events = RecordingEvents()
    service = TaskService(RecordingRepository(), events)
    checklist = service.create_checklist("typed", ["inspect", "test"])
    assert checklist.summary == "0/2 complete"
    assert events.rows == []
    service.publish_checklist(checklist)
    assert events.rows[0]["summary"] == "0/2 complete"


def test_application_validation_and_not_found_errors_are_typed():
    service = TaskService(RecordingRepository(), RecordingEvents())
    with pytest.raises(InvalidInput, match="Expecting value") as invalid:
        service.create_checklist("invalid", "not-json")
    assert invalid.value.code == "INVALID_INPUT"

    with pytest.raises(NotFound, match="no checklist 'missing'") as missing:
        service.checklist("missing")
    assert missing.value.code == "NOT_FOUND"


def test_legacy_repository_maps_validation_and_storage_errors(tmp_path):
    connection = memory_store.connect(str(tmp_path / "typed-errors.db"))
    repository = LegacyTaskRepository(connection)
    try:
        with pytest.raises(InvalidInput, match="task title is required"):
            repository.create(title="")
        with pytest.raises(NotFound, match="no unique task 'missing'"):
            repository.update("missing", status="done")
    finally:
        connection.close()

    with pytest.raises(DependencyUnavailable):
        repository.list()
