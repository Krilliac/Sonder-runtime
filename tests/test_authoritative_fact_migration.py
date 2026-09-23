from __future__ import annotations

import sqlite3

import pytest

from sonder_runtime.adapters.memory_store import connect, facts_for_project
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    migrate_legacy_facts,
    plan_legacy_fact_migration,
)
from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    SQLiteMemoryReplicationJournal,
)
from sonder_runtime.domain.memory.replication import MemoryReplicationError


def test_legacy_migration_plan_is_dry_run_and_atomic(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    plan = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    assert plan.rows == (("legacy", "repo-a", "old fact", None),)
    assert connection.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    assert migrate_legacy_facts(connection, plan, backup_path=tmp_path / "backup.db") == 1
    connection.close()

    reopened = connect(path)
    try:
        assert facts_for_project(reopened, "repo-a")[0]["id"] == "legacy"
        assert tuple(reopened.execute(
            "SELECT source_id,version,tombstoned FROM memory_authoritative_fact_state"
        ).fetchone()) == ("node-a", 1, 0)
        assert reopened.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 1
    finally:
        reopened.close()
    assert (tmp_path / "backup.db").exists()


def test_legacy_migration_rejects_stale_plan_without_mutation(tmp_path):
    connection = connect(tmp_path / "memory.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    plan = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    connection.execute("UPDATE facts SET text=? WHERE id=?", ("changed", "legacy"))
    connection.commit()
    with pytest.raises(MemoryReplicationError, match="stale"):
        migrate_legacy_facts(connection, plan)
    assert connection.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    connection.close()


def test_legacy_migration_rolls_back_fact_state_journal_and_indexes(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence.sqlite import authoritative_memory

    connection = connect(tmp_path / "memory.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    plan = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")

    def fail_append(*args, **kwargs):
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(authoritative_memory, "append_memory_mutations_in_transaction", fail_append)
    with pytest.raises(RuntimeError, match="injected"):
        migrate_legacy_facts(connection, plan)
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_authoritative_fact_state"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_authoritative_entity_index"
    ).fetchone()[0] == 0
    assert facts_for_project(connection, "repo-a")[0]["text"] == "old fact"
    connection.close()


def test_migration_replay_is_idempotent_only_after_plan_is_empty(tmp_path):
    connection = connect(tmp_path / "memory.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    plan = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    migrate_legacy_facts(connection, plan)
    empty = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    assert empty.rows == ()
    assert migrate_legacy_facts(connection, empty) == 0
    connection.close()


def test_migration_keeps_tombstones_and_conflicting_ownership_fail_closed(tmp_path):
    connection = connect(tmp_path / "memory.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    first = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    migrate_legacy_facts(connection, first)
    source = SQLiteMemoryReplicationJournal(path=tmp_path / "memory.db", source_id="node-a", project_scope="repo-a")
    source.close()
    connection.execute(
        "DELETE FROM facts WHERE id=?", ("legacy",)
    )
    connection.commit()
    assert plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a").rows == ()
    connection.close()
