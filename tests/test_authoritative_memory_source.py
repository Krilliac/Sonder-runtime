"""Atomic source-side evidence for the deliberately narrow fact write set."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.embeddings import from_blob, to_blob
from sonder_runtime.adapters.memory_store import connect, facts_for_project
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    SQLiteAuthoritativeFactSource,
)
from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    SQLiteMemoryReplicationJournal,
)
from sonder_runtime.adapters.persistence.sqlite.memory_projection import (
    SQLiteMemoryReplicationProjection,
)
from sonder_runtime.domain.memory.replication import MemoryReplicationError


def test_authoritative_fact_source_commits_fact_and_journal_record_together(tmp_path):
    """The supported live set is one project-scoped ``fact`` entity kind."""
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    record = source.add_fact(
        connection,
        "fact-1",
        "repo-a",
        "Use focused pytest regressions.",
        None,
    )

    assert facts_for_project(connection, "repo-a") == [
        {
            "id": "fact-1",
            "project": "repo-a",
            "text": "Use focused pytest regressions.",
            "embedding": None,
        }
    ]
    assert (
        record.source_id,
        record.source_epoch,
        record.sequence,
        record.entity_kind,
        record.entity_id,
        record.version,
        record.operation,
        record.project,
        dict(record.payload),
    ) == (
        "node-a",
        1,
        1,
        "fact",
        "fact-1",
        1,
        "upsert",
        "repo-a",
        {"text": "Use focused pytest regressions.", "embedding": None},
    )
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert journal.export().records == (record,)
    finally:
        journal.close()


def test_authoritative_fact_source_advances_entity_version_on_explicit_upsert(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(connection, "fact-1", "repo-a", "first value")

    updated = source.upsert_fact(
        connection,
        "fact-1",
        "repo-a",
        "second value",
    )

    assert updated.sequence == 2
    assert updated.version == 2
    assert updated.operation == "upsert"
    assert facts_for_project(connection, "repo-a")[0]["text"] == "second value"
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert [record.version for record in journal.export(limit=10).records] == [1, 2]
    finally:
        journal.close()


def test_authoritative_fact_source_emits_a_projection_safe_live_embedding(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    record = source.add_fact(
        connection,
        "fact-1",
        "repo-a",
        "embedded fact",
        to_blob([1.0, -0.5]),
    )

    assert list(record.payload["embedding"]) == pytest.approx([1.0, -0.5])
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    target = connect(tmp_path / "target.db")
    try:
        assert SQLiteMemoryReplicationProjection(target).apply(journal.export()) == 1
        assert from_blob(facts_for_project(target, "repo-a")[0]["embedding"]) == pytest.approx(
            [1.0, -0.5]
        )
    finally:
        target.close()
        journal.close()


def test_authoritative_fact_source_deletion_commits_a_tombstone(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(connection, "fact-1", "repo-a", "obsolete value")

    assert source.delete_fact(connection, "fact-1", "repo-a") is True
    assert facts_for_project(connection, "repo-a") == []
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        records = journal.export(limit=10).records
        assert [(record.sequence, record.version, record.operation, dict(record.payload)) for record in records] == [
            (1, 1, "upsert", {"text": "obsolete value", "embedding": None}),
            (2, 2, "delete", {}),
        ]
        assert journal.tombstones() == (records[-1],)
    finally:
        journal.close()


def test_authoritative_fact_source_rolls_back_materialized_fact_when_journal_rejects(
    tmp_path,
):
    connection = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    connection.execute(
        "CREATE TRIGGER reject_authoritative_journal "
        "BEFORE INSERT ON memory_replication_log BEGIN "
        "SELECT RAISE(ABORT, 'journal unavailable'); END"
    )

    with pytest.raises(MemoryReplicationError):
        source.add_fact(connection, "fact-1", "repo-a", "must not persist")

    assert facts_for_project(connection, "repo-a") == []
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_log"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_authoritative_fact_state"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_meta"
    ).fetchone()[0] == 0
    connection.close()


def test_unit_of_work_routes_an_explicit_authoritative_fact_source(tmp_path):
    path = tmp_path / "memory.db"
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    with UnitOfWorkAdapter(
        str(path), authoritative_fact_source=source
    ) as unit_of_work:
        assert unit_of_work.memory.add_fact(
            "fact-1", "repo-a", "live source write"
        ) is None
        assert unit_of_work.memory.delete_fact("fact-1", "repo-a") is True

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert [(record.sequence, record.version, record.operation) for record in journal.export(limit=10).records] == [
            (1, 1, "upsert"),
            (2, 2, "delete"),
        ]
    finally:
        journal.close()


def test_authoritative_fact_uow_rolls_back_source_state_after_later_failure(tmp_path):
    """An injected source must share the UoW's rollback boundary."""
    path = tmp_path / "memory.db"
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    with pytest.raises(RuntimeError, match="abort unit of work"):
        with UnitOfWorkAdapter(
            str(path), authoritative_fact_source=source
        ) as unit_of_work:
            unit_of_work.memory.add_fact(
                "fact-1", "repo-a", "must roll back with the unit of work"
            )
            raise RuntimeError("abort unit of work")

    connection = connect(path)
    try:
        assert facts_for_project(connection, "repo-a") == []
        for table in (
            "memory_authoritative_fact_state",
            "memory_replication_log",
            "memory_replication_meta",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0
    finally:
        connection.close()


def test_default_unit_of_work_preserves_the_legacy_unjournaled_fact_path(tmp_path):
    path = tmp_path / "memory.db"

    with UnitOfWorkAdapter(str(path)) as unit_of_work:
        unit_of_work.memory.add_fact("fact-1", "repo-a", "legacy fact")

    connection = connect(path)
    try:
        assert facts_for_project(connection, "repo-a")[0]["text"] == "legacy fact"
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_replication_meta"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM memory_replication_log"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_authoritative_fact_source_persists_an_explicit_epoch_advance(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    source.advance_epoch(connection, 2)
    record = source.add_fact(connection, "fact-1", "repo-a", "new epoch fact")

    assert (record.source_epoch, record.sequence, record.version) == (2, 1, 1)
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert journal.export().source_epoch == 2
    finally:
        journal.close()


def test_authoritative_fact_source_rejects_epoch_rollover_after_a_mutation(tmp_path):
    connection = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(connection, "fact-1", "repo-a", "existing fact")

    with pytest.raises(MemoryReplicationError, match="empty"):
        source.advance_epoch(connection, 2)

    assert connection.execute(
        "SELECT source_epoch FROM memory_replication_meta WHERE source_id=?",
        ("node-a",),
    ).fetchone()[0] == 1
    connection.close()


def test_authoritative_fact_source_rejects_epoch_rollover_after_full_prune(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(connection, "fact-1", "repo-a", "first value")
    assert source.delete_fact(connection, "fact-1", "repo-a") is True
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert journal.prune_before(3, retain_tombstones=False) == 2
        assert journal.export().records == ()
    finally:
        journal.close()

    reopened = connect(path)
    try:
        with pytest.raises(MemoryReplicationError, match="bootstrap"):
            source.advance_epoch(reopened, 2)
        assert tuple(
            reopened.execute(
                "SELECT source_epoch,next_sequence FROM memory_replication_meta "
                "WHERE source_id=?",
                ("node-a",),
            ).fetchone()
        ) == (1, 3)
    finally:
        reopened.close()


def test_authoritative_fact_source_refuses_a_second_source_for_the_same_fact(tmp_path):
    connection = connect(tmp_path / "memory.db")
    first = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    second = SQLiteAuthoritativeFactSource("node-b", project_scope="repo-a")
    first.add_fact(connection, "fact-1", "repo-a", "node a owns this")

    with pytest.raises(MemoryReplicationError, match="another source"):
        second.upsert_fact(connection, "fact-1", "repo-a", "node b cannot replace it")

    assert facts_for_project(connection, "repo-a")[0]["text"] == "node a owns this"
    owner = connection.execute(
        "SELECT source_id,version,tombstoned "
        "FROM memory_authoritative_fact_state WHERE project=? AND fact_id=?",
        ("repo-a", "fact-1"),
    ).fetchone()
    assert tuple(owner) == ("node-a", 1, 0)
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_log WHERE source_id=?",
        ("node-b",),
    ).fetchone()[0] == 0
    connection.close()


def test_authoritative_fact_source_never_widens_its_project_scope(tmp_path):
    connection = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")

    with pytest.raises(MemoryReplicationError, match="scope"):
        source.add_fact(connection, "fact-1", "repo-b", "wrong project")

    assert facts_for_project(connection, "repo-a") == []
    assert facts_for_project(connection, "repo-b") == []
    assert connection.execute(
        "SELECT COUNT(*) FROM memory_replication_meta"
    ).fetchone()[0] == 0
    connection.close()


def test_authoritative_fact_state_keeps_versions_after_explicit_journal_pruning(tmp_path):
    path = tmp_path / "memory.db"
    connection = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(connection, "fact-1", "repo-a", "first")
    assert source.delete_fact(connection, "fact-1", "repo-a") is True
    connection.close()

    journal = SQLiteMemoryReplicationJournal(
        path,
        source_id="node-a",
        project_scope="repo-a",
    )
    try:
        assert journal.prune_before(3, retain_tombstones=False) >= 1
    finally:
        journal.close()

    reopened = connect(path)
    restored = source.upsert_fact(reopened, "fact-1", "repo-a", "restored")
    assert (restored.sequence, restored.version) == (3, 3)
    reopened.close()
