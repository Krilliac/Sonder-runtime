import pytest
import sqlite3

from sonder_runtime.adapters.persistence.durable_continuation import SQLiteDurableContinuationRepository
from sonder_runtime.application.ports.continuation_records import ChildSessionLineage, DurableChildSession
from sonder_runtime.application.ports.subagents import InvalidSubagentRequest, SubagentBudget, SubagentRequest, SubagentStatus


def session(child, *, parent="parent", resume_key="", idempotency_key=""):
    request = SubagentRequest(parent, "task", SubagentBudget(max_steps=2), child, (), resume_key, idempotency_key)
    return DurableChildSession(request, ChildSessionLineage("parent"))


def test_active_resume_key_is_rejected_inside_durable_create(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "keys.sqlite")
    repository.create(session("one", resume_key="resume-1", idempotency_key="idem-1"))
    with pytest.raises(InvalidSubagentRequest, match="active child"):
        repository.create(session("two", resume_key="resume-1", idempotency_key="idem-2"))
    with pytest.raises(InvalidSubagentRequest, match="active child"):
        repository.create(session("three", resume_key="resume-3", idempotency_key="idem-1"))
    assert repository.get("one").status is SubagentStatus.CREATED


def test_keys_survive_repository_restart_and_terminal_keys_can_be_reused(tmp_path):
    path = tmp_path / "restart.sqlite"
    first = SQLiteDurableContinuationRepository(path)
    first.create(session("one", resume_key="resume-1", idempotency_key="idem-1"))
    restored = SQLiteDurableContinuationRepository(path).get("one")
    assert restored and restored.request.resume_key == "resume-1"
    assert restored.request.idempotency_key == "idem-1"
    terminal = first.update("one", status=SubagentStatus.FAILED, recovery_required=True)
    assert terminal and terminal.status is SubagentStatus.FAILED
    second = SQLiteDurableContinuationRepository(path)
    second.create(session("two", resume_key="resume-1", idempotency_key="idem-2"))
    assert second.get("two").request.resume_key == "resume-1"


def test_key_namespaces_and_parent_scope_do_not_collide(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "scope.sqlite")
    repository.create(session("one", resume_key="same", idempotency_key="idem"))
    repository.create(session("two", parent="other-parent", resume_key="same", idempotency_key="idem"))
    repository.create(session("three", idempotency_key="same"))
    with pytest.raises(InvalidSubagentRequest, match="resume key"):
        repository.create(session("four", resume_key="same"))


def test_existing_pre_key_table_is_migrated_without_losing_rows(tmp_path):
    path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE durable_child_session (child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, ancestors_json TEXT NOT NULL, prompt TEXT NOT NULL, budget_json TEXT NOT NULL, metadata_json TEXT NOT NULL, status TEXT NOT NULL, checkpoint_sequence INTEGER, checkpoint_state_json TEXT, checkpoint_cursor TEXT, revision INTEGER NOT NULL, usage_json TEXT NOT NULL, result_json TEXT, recovery_required INTEGER NOT NULL, cancellation_requested INTEGER NOT NULL, cancellation_reason TEXT)")
    connection.execute("INSERT INTO durable_child_session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("legacy", "parent", "[]", "task", '{"max_steps":2,"max_children":null,"max_depth":null,"max_concurrency":null,"max_wall_seconds":null,"max_output_tokens":null}', "[]", "created", None, None, None, 0, '{"steps":0,"output_tokens":null,"wall_seconds":null}', None, 0, 0, None))
    connection.commit(); connection.close()
    repository = SQLiteDurableContinuationRepository(path)
    restored = repository.get("legacy")
    assert restored and restored.request.resume_key == ""
    repository.create(session("new", resume_key="legacy-key"))
