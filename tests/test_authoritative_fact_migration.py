from __future__ import annotations

import sqlite3
import json
import struct
from dataclasses import replace
import sys

import pytest

from sonder_runtime.adapters.memory_store import connect, facts_for_project
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    SQLiteAuthoritativeFactSource,
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
        assert tuple(reopened.execute(
            "SELECT source_id,project_scope FROM memory_authoritative_fact_activation"
        ).fetchone()) == ("node-a", "repo-a")
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
        migrate_legacy_facts(connection, plan, backup_path=tmp_path / "stale-backup.db")
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
        migrate_legacy_facts(connection, plan, backup_path=tmp_path / "failure-backup.db")
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_authoritative_fact_state"
    ).fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_authoritative_entity_index"
    ).fetchone()[0] == 0
    assert facts_for_project(connection, "repo-a")[0]["text"] == "old fact"
    connection.close()


def test_interrupted_migration_releases_write_lock_and_can_resume(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence.sqlite import authoritative_memory

    path = tmp_path / "interrupted.db"
    connection = connect(path)
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    plan = plan_legacy_fact_migration(
        connection, source_id="node-a", project_scope="repo-a",
    )

    original_append = authoritative_memory.append_memory_mutations_in_transaction

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("simulated operator interruption")

    monkeypatch.setattr(
        authoritative_memory, "append_memory_mutations_in_transaction", interrupt,
    )
    with pytest.raises(KeyboardInterrupt, match="simulated"):
        migrate_legacy_facts(
            connection, plan, backup_path=tmp_path / "interrupted-backup.db",
        )

    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_log"
    ).fetchone()[0] == 0
    assert facts_for_project(connection, "repo-a")[0]["text"] == "old fact"

    monkeypatch.setattr(
        authoritative_memory, "append_memory_mutations_in_transaction",
        original_append,
    )
    assert migrate_legacy_facts(
        connection, plan, backup_path=tmp_path / "resumed-backup.db",
    ) == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_log"
    ).fetchone()[0] == 1
    connection.close()


def test_migration_replay_is_idempotent_only_after_plan_is_empty(tmp_path):
    connection = connect(tmp_path / "memory.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    plan = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    migrate_legacy_facts(connection, plan, backup_path=tmp_path / "first-backup.db")
    empty = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    assert empty.rows == ()
    assert migrate_legacy_facts(connection, empty, backup_path=tmp_path / "empty-backup.db") == 0
    connection.close()


def test_migration_keeps_tombstones_and_conflicting_ownership_fail_closed(tmp_path):
    connection = connect(tmp_path / "memory.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    first = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    migrate_legacy_facts(connection, first, backup_path=tmp_path / "tombstone-backup.db")
    source = SQLiteMemoryReplicationJournal(path=tmp_path / "memory.db", source_id="node-a", project_scope="repo-a")
    source.close()
    connection.execute(
        "DELETE FROM facts WHERE id=?", ("legacy",)
    )
    connection.commit()
    with pytest.raises(MemoryReplicationError, match="missing authoritative journal evidence"):
        plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    connection.close()


def test_migration_digest_binds_source_and_project_scope(tmp_path):
    connection = connect(tmp_path / "memory.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    plan = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    assert plan.digest != plan_legacy_fact_migration(
        connection, source_id="node-b", project_scope="repo-a"
    ).digest
    assert plan.digest != plan_legacy_fact_migration(
        connection, source_id="node-a", project_scope="repo-b"
    ).digest
    with pytest.raises(MemoryReplicationError, match="stale"):
        migrate_legacy_facts(connection, replace(plan, source_id="node-b"))
    connection.close()


@pytest.mark.parametrize(
    ("source_id", "state_source", "expected"),
    [
        ("node-a", "node-b", "conflicting authoritative ownership"),
        ("node-a", "node-a", "missing authoritative journal evidence"),
    ],
)
def test_migration_rejects_existing_state_without_matching_authority_evidence(
    tmp_path, source_id, state_source, expected,
):
    connection = connect(tmp_path / f"inconsistent-{state_source}.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("fact-1", "repo-a", "already materialized", None),
    )
    connection.execute(
        "INSERT INTO memory_authoritative_fact_state"
        "(project,fact_id,source_id,version,tombstoned) VALUES(?,?,?,?,?)",
        ("repo-a", "fact-1", state_source, 1, 0),
    )
    connection.commit()

    with pytest.raises(MemoryReplicationError, match=expected):
        plan_legacy_fact_migration(
            connection, source_id=source_id, project_scope="repo-a",
        )
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_log"
    ).fetchone()[0] == 0
    connection.close()


def test_migration_rejects_foreign_tombstone_without_a_fact_row(tmp_path):
    connection = connect(tmp_path / "foreign-tombstone.db")
    connection.execute(
        "INSERT INTO memory_authoritative_fact_state"
        "(project,fact_id,source_id,version,tombstoned) VALUES(?,?,?,?,?)",
        ("repo-a", "gone", "node-b", 2, 1),
    )
    connection.commit()
    with pytest.raises(MemoryReplicationError, match="conflicting authoritative ownership"):
        plan_legacy_fact_migration(
            connection, source_id="node-a", project_scope="repo-a",
        )
    connection.close()


def test_migration_accepts_same_source_tombstone_with_delete_evidence(tmp_path):
    connection = connect(tmp_path / "same-source-tombstone.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.activate(connection)
    source.add_fact(connection, "gone", "repo-a", "temporary")
    assert source.delete_fact(connection, "gone", "repo-a") is True

    plan = plan_legacy_fact_migration(
        connection, source_id="node-a", project_scope="repo-a",
    )
    assert plan.rows == ()
    connection.close()


def test_migration_rejects_same_source_tombstone_without_delete_evidence(tmp_path):
    connection = connect(tmp_path / "missing-delete-evidence.db")
    connection.execute(
        "INSERT INTO memory_authoritative_fact_state"
        "(project,fact_id,source_id,version,tombstoned) VALUES(?,?,?,?,?)",
        ("repo-a", "gone", "node-a", 2, 1),
    )
    connection.commit()
    with pytest.raises(MemoryReplicationError, match="missing authoritative journal evidence"):
        plan_legacy_fact_migration(
            connection, source_id="node-a", project_scope="repo-a",
        )
    connection.close()


@pytest.mark.parametrize("corruption", ["text", "embedding", "digest"])
def test_migration_rejects_corrupt_live_journal_proof(tmp_path, corruption):
    connection = connect(tmp_path / f"corrupt-{corruption}.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.activate(connection)
    source.add_fact(connection, "fact-1", "repo-a", "canonical")
    if corruption == "text":
        connection.execute("UPDATE facts SET text=? WHERE id=?", ("tampered", "fact-1"))
    elif corruption == "embedding":
        connection.execute(
            "UPDATE facts SET embedding=? WHERE id=?",
            (sqlite3.Binary(struct.pack("<f", 1.0)), "fact-1"),
        )
    else:
        connection.execute(
            "UPDATE memory_replication_log SET digest=? WHERE entity_id=?",
            ("0" * 64, "fact-1"),
        )
    connection.commit()
    with pytest.raises(MemoryReplicationError, match="authoritative journal evidence"):
        plan_legacy_fact_migration(
            connection, source_id="node-a", project_scope="repo-a",
        )
    connection.close()


@pytest.mark.parametrize("corruption", ["payload", "version"])
def test_migration_rejects_corrupt_tombstone_proof(tmp_path, corruption):
    connection = connect(tmp_path / f"corrupt-tombstone-{corruption}.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.activate(connection)
    source.add_fact(connection, "gone", "repo-a", "temporary")
    source.delete_fact(connection, "gone", "repo-a")
    if corruption == "payload":
        connection.execute(
            "UPDATE memory_replication_log SET payload_json=? WHERE entity_id=?",
            ('{"text":"forged"}', "gone"),
        )
    else:
        connection.execute(
            "UPDATE memory_authoritative_fact_state SET version=? WHERE fact_id=?",
            (3, "gone"),
        )
    connection.commit()
    with pytest.raises(MemoryReplicationError, match="authoritative journal evidence"):
        plan_legacy_fact_migration(
            connection, source_id="node-a", project_scope="repo-a",
        )
    connection.close()


def test_migration_requires_idle_connection(tmp_path):
    connection = connect(tmp_path / "memory.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    plan = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")
    connection.execute("BEGIN")
    with pytest.raises(MemoryReplicationError, match="idle connection"):
        migrate_legacy_facts(connection, plan)
    connection.rollback()
    connection.close()


def test_migration_rechecks_after_backup_race(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    racer = connect(path)
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    plan = plan_legacy_fact_migration(connection, source_id="node-a", project_scope="repo-a")

    class RacingConnection:
        def __init__(self, inner):
            self.inner = inner
            self.in_transaction = inner.in_transaction

        def execute(self, *args, **kwargs):
            return self.inner.execute(*args, **kwargs)

        def backup(self, target):
            result = self.inner.backup(target)
            racer.execute("UPDATE facts SET text=? WHERE id=?", ("raced", "legacy"))
            racer.commit()
            return result

        def commit(self):
            return self.inner.commit()

        def rollback(self):
            return self.inner.rollback()

    raced = RacingConnection(connection)
    with pytest.raises(MemoryReplicationError, match="stale"):
        migrate_legacy_facts(raced, plan, backup_path=tmp_path / "backup.db")
    assert connection.execute("SELECT text FROM facts WHERE id=?", ("legacy",)).fetchone()[0] == "raced"
    assert connection.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    racer.close()
    connection.close()


def test_plan_rejects_invalid_identity_and_oversized_legacy_bytes(tmp_path):
    connection = connect(tmp_path / "memory.db")
    with pytest.raises(MemoryReplicationError):
        plan_legacy_fact_migration(
            connection, source_id="bad source", project_scope="repo-a",
        )
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,zeroblob(?))",
        ("large", "repo-a", "legacy", 32 * 1024 * 1024 + 1),
    )
    connection.commit()
    with pytest.raises(MemoryReplicationError, match="byte limit"):
        plan_legacy_fact_migration(
            connection, source_id="node-a", project_scope="repo-a",
        )
    connection.close()


def test_apply_requires_backup_and_never_clobbers_existing_path(tmp_path):
    connection = connect(tmp_path / "memory.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "old fact", None),
    )
    connection.commit()
    plan = plan_legacy_fact_migration(
        connection, source_id="node-a", project_scope="repo-a",
    )
    with pytest.raises(MemoryReplicationError, match="backup path"):
        migrate_legacy_facts(connection, plan)
    backup = tmp_path / "backup.db"
    backup.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(MemoryReplicationError, match="already exists"):
        migrate_legacy_facts(connection, plan, backup_path=backup)
    assert backup.read_text(encoding="utf-8") == "do not overwrite"
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_log"
    ).fetchone()[0] == 0
    connection.close()


def test_operator_command_does_not_create_a_missing_database(tmp_path, monkeypatch):
    from scripts import migrate_authoritative_facts as command

    missing = tmp_path / "missing.db"
    monkeypatch.setattr(sys, "argv", [
        "migrate_authoritative_facts.py", "--database", str(missing),
        "--source-id", "node-a", "--project", "repo-a",
    ])
    with pytest.raises(SystemExit) as stopped:
        command.main()
    assert stopped.value.code == 2
    assert not missing.exists()


def test_operator_refuses_stale_schema_before_dry_run_or_backup(tmp_path, monkeypatch, capsys):
    from scripts import migrate_authoritative_facts as command

    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE facts(id TEXT PRIMARY KEY, project TEXT, text TEXT, embedding BLOB)"
    )
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "original", None),
    )
    connection.commit()
    connection.close()
    before = database.read_bytes()
    base = [
        "migrate_authoritative_facts.py", "--database", str(database),
        "--source-id", "node-a", "--project", "repo-a",
    ]

    monkeypatch.setattr(sys, "argv", base)
    with pytest.raises(SystemExit) as dry_run:
        command.main()
    assert dry_run.value.code == 2
    assert "schema is not current" in capsys.readouterr().err
    assert database.read_bytes() == before

    backup = tmp_path / "backup.db"
    monkeypatch.setattr(
        sys, "argv", base + ["--apply", "--digest", "unapproved", "--backup", str(backup)],
    )
    with pytest.raises(SystemExit) as apply:
        command.main()
    assert apply.value.code == 2
    assert not backup.exists()
    assert database.read_bytes() == before


def test_operator_dry_run_then_explicit_apply_on_current_schema(tmp_path, monkeypatch, capsys):
    from scripts import migrate_authoritative_facts as command

    database = tmp_path / "memory.db"
    connection = connect(database)
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        ("legacy", "repo-a", "exact original", None),
    )
    connection.commit()
    connection.close()
    base = [
        "migrate_authoritative_facts.py", "--database", str(database),
        "--source-id", "node-a", "--project", "repo-a",
    ]

    monkeypatch.setattr(sys, "argv", base)
    assert command.main() == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["count"] == 1
    readback = sqlite3.connect(database)
    assert readback.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    readback.close()

    backup = tmp_path / "backup.db"
    monkeypatch.setattr(
        sys, "argv", base + ["--apply", "--digest", planned["digest"], "--backup", str(backup)],
    )
    assert command.main() == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["migrated"] == 1
    assert backup.exists()
    snapshot = sqlite3.connect(backup)
    assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert snapshot.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    snapshot.close()
    current = sqlite3.connect(database)
    assert current.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 1
    current.close()


@pytest.mark.parametrize(
    "fact_id,text,embedding,expected",
    [
        ("legacy", None, None, "text"),
        ("legacy", sqlite3.Binary(b"bytes"), None, "text"),
        (None, "valid text", None, "ID"),
        ("legacy", "valid text", "not a blob", "embedding"),
        ("legacy", "valid text", 10_000_000, "embedding"),
    ],
)
def test_plan_refuses_malformed_storage_types_without_coercion(
    tmp_path, fact_id, text, embedding, expected,
):
    connection = connect(tmp_path / "memory.db")
    connection.execute(
        "INSERT INTO facts(id,project,text,embedding) VALUES(?,?,?,?)",
        (fact_id, "repo-a", text, embedding),
    )
    connection.commit()
    with pytest.raises(MemoryReplicationError, match=expected):
        plan_legacy_fact_migration(
            connection, source_id="node-a", project_scope="repo-a",
        )
    assert connection.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    connection.close()
