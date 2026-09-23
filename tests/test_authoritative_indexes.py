import pytest

from sonder_runtime.adapters.memory_store import connect
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    AuthoritativeFactMetadata,
    SQLiteAuthoritativeFactSource,
)
from sonder_runtime.adapters.persistence.sqlite.authoritative_indexes import (
    decisions_for_project,
    entities_for_project,
    rebuild_authoritative_fact_indexes,
)
from sonder_runtime.domain.memory.replication import MemoryReplicationError


def _metadata():
    return AuthoritativeFactMetadata(
        entities=("parser", "release"),
        decision={"id": "parser-policy", "value": "prefer bounded parsing"},
        valid_from="2026-01-01T00:00:00+00:00",
        provenance=("review:case-17",),
    )


def test_scoped_entity_and_decision_indexes_survive_restart_and_rebuild(tmp_path):
    path = tmp_path / "memory.db"
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    conn = connect(path)
    source.add_fact(conn, "fact-1", "repo-a", "bounded parsing", metadata=_metadata())
    assert len(entities_for_project(conn, "repo-a", now="2026-02-01T00:00:00+00:00")) == 2
    assert decisions_for_project(conn, "repo-b") == []
    conn.close()

    reopened = connect(path)
    try:
        assert entities_for_project(reopened, "repo-a")[0]["source_id"] == "node-a"
        reopened.execute("DELETE FROM memory_authoritative_entity_index")
        reopened.execute("DELETE FROM memory_authoritative_decision_index")
        assert rebuild_authoritative_fact_indexes(reopened, project="repo-a") == 1
        assert decisions_for_project(reopened, "repo-a")[0]["decision_id"] == "parser-policy"
    finally:
        reopened.close()


def test_tombstone_excludes_stale_indexes_but_preserves_versioned_source(tmp_path):
    path = tmp_path / "memory.db"
    conn = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(conn, "fact-1", "repo-a", "old", metadata=_metadata())
    assert source.delete_fact(conn, "fact-1", "repo-a") is True
    assert entities_for_project(conn, "repo-a") == []
    assert tuple(conn.execute(
        "SELECT tombstoned,version FROM memory_authoritative_entity_index "
        "WHERE project=? AND fact_id=? AND entity_id=?",
        ("repo-a", "fact-1", "parser"),
    ).fetchone()) == (1, 2)
    conn.close()


def test_index_failure_rolls_back_fact_journal_and_materialization(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence.sqlite import authoritative_memory

    conn = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    monkeypatch.setattr(
        authoritative_memory,
        "materialize_authoritative_fact_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("index failed")),
    )
    with pytest.raises(RuntimeError, match="index failed"):
        source.add_fact(conn, "fact-1", "repo-a", "must roll back", metadata=_metadata())
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memory_authoritative_fact_state").fetchone()[0] == 0


def test_metadata_is_typed_and_legacy_path_cannot_silently_drop_it(tmp_path):
    from sonder_runtime.adapters.memory_repository import MemoryRepositoryAdapter

    conn = connect(tmp_path / "memory.db")
    repository = MemoryRepositoryAdapter(conn)
    with pytest.raises(ValueError, match="configured fact source"):
        repository.add_fact("fact-1", "repo-a", "tagged", metadata=_metadata())
    with pytest.raises(MemoryReplicationError, match="typed authoritative"):
        SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a").add_fact(
            conn, "fact-1", "repo-a", "bad", metadata={"entities": ("x",)}
        )

