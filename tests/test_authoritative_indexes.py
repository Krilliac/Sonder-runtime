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


def test_rebuild_keeps_existing_indexes_when_source_journal_is_changed(tmp_path):
    path = tmp_path / "memory.db"
    conn = connect(path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(conn, "fact-1", "repo-a", "original", metadata=_metadata())
    before = entities_for_project(conn, "repo-a")
    conn.execute(
        "UPDATE memory_replication_log SET payload_json=? WHERE source_id=? AND sequence=?",
        ('{"text":"changed","embedding":null}', "node-a", 1),
    )
    conn.commit()
    with pytest.raises(MemoryReplicationError, match="changed journal"):
        rebuild_authoritative_fact_indexes(conn, project="repo-a")
    assert entities_for_project(conn, "repo-a") == before
    conn.close()


def test_rebuild_row_bound_refuses_before_clearing_indexes(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence.sqlite import authoritative_indexes

    conn = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(conn, "fact-1", "repo-a", "original", metadata=_metadata())
    before = entities_for_project(conn, "repo-a")
    monkeypatch.setattr(authoritative_indexes, "_MAX_REBUILD_ROWS", 0)
    with pytest.raises(MemoryReplicationError, match="row bound"):
        rebuild_authoritative_fact_indexes(conn, project="repo-a")
    assert entities_for_project(conn, "repo-a") == before
    conn.close()


def test_indexed_fact_metadata_normalizes_time_and_snapshots_decision(tmp_path):
    decision = {"id": "rule", "value": "first"}
    metadata = AuthoritativeFactMetadata(
        entities=("parser",), decision=decision,
        valid_from="2026-01-01T05:00:00+05:00",
        provenance=("host:receipt-1",),
    )
    conn = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(conn, "fact-1", "repo-a", "bounded parser", metadata=metadata)
    decision["value"] = "changed after commit"
    row = decisions_for_project(conn, "repo-a", now="2026-01-01T00:00:00+00:00")[0]
    assert row["valid_from"] == "2026-01-01T00:00:00+00:00"
    assert '"value":"first"' in row["decision_json"]
    conn.close()


def test_indexed_fact_rejects_unscoped_or_invalid_temporal_claims():
    with pytest.raises(MemoryReplicationError, match="timezone"):
        AuthoritativeFactMetadata(
            entities=("parser",), valid_from="2026-01-01T00:00:00",
            provenance=("host:receipt-1",),
        )
    with pytest.raises(MemoryReplicationError, match="must follow"):
        AuthoritativeFactMetadata(
            entities=("parser",), valid_from="2026-01-02T00:00:00Z",
            valid_until="2026-01-01T00:00:00Z", provenance=("host:receipt-1",),
        )
    with pytest.raises(MemoryReplicationError, match="explicit provenance"):
        AuthoritativeFactMetadata(entities=("parser",))


def test_scoped_index_retrieval_pages_without_silent_loss(tmp_path):
    conn = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    for index in range(17):
        source.add_fact(
            conn, f"fact-{index:02d}", "repo-a", f"fact {index}",
            metadata=AuthoritativeFactMetadata(
                entities=("parser",), provenance=("host:receipt",),
            ),
        )
    first = entities_for_project(conn, "repo-a", entity_id="parser")
    second = entities_for_project(conn, "repo-a", entity_id="parser", offset=16)
    assert len(first) == 16 and len(second) == 1
    assert {row["fact_id"] for row in first + second} == {
        f"fact-{index:02d}" for index in range(17)
    }
    with pytest.raises(ValueError, match="offset"):
        entities_for_project(conn, "repo-a", offset=-1)
    conn.close()


def test_superseding_decision_hides_stale_entity_and_decision_at_valid_time(tmp_path):
    conn = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(conn, "old", "repo-a", "old policy", metadata=AuthoritativeFactMetadata(
        entities=("parser",), decision={"id": "parser-policy", "value": "old"},
        valid_from="2026-01-01T00:00:00Z", provenance=("review:old",),
    ))
    source.add_fact(conn, "new", "repo-a", "new policy", metadata=AuthoritativeFactMetadata(
        decision={"id": "parser-policy", "value": "new"}, supersedes="old",
        valid_from="2026-02-01T00:00:00Z", provenance=("review:new",),
    ))
    january = "2026-01-15T00:00:00Z"
    february = "2026-02-15T00:00:00Z"
    assert [row["fact_id"] for row in entities_for_project(conn, "repo-a", now=january)] == ["old"]
    assert [row["fact_id"] for row in decisions_for_project(conn, "repo-a", now=january)] == ["old"]
    assert entities_for_project(conn, "repo-a", now=february) == []
    assert [row["fact_id"] for row in decisions_for_project(conn, "repo-a", now=february)] == ["new"]

    assert rebuild_authoritative_fact_indexes(conn, project="repo-a") == 2
    assert entities_for_project(conn, "repo-a", now=february) == []
    assert [row["fact_id"] for row in decisions_for_project(conn, "repo-a", now=february)] == ["new"]
    assert source.delete_fact(conn, "new", "repo-a") is True
    assert entities_for_project(conn, "repo-a", now=february) == []
    assert decisions_for_project(conn, "repo-a", now=february) == []
    conn.close()


def test_supersession_does_not_cross_project_scope(tmp_path):
    conn = connect(tmp_path / "memory.db")
    first = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    second = SQLiteAuthoritativeFactSource("node-b", project_scope="repo-b")
    first.add_fact(conn, "old", "repo-a", "repo a policy", metadata=AuthoritativeFactMetadata(
        entities=("parser",), provenance=("review:a",),
    ))
    with pytest.raises(MemoryReplicationError, match="same source and project"):
        second.add_fact(conn, "new", "repo-b", "repo b policy", metadata=AuthoritativeFactMetadata(
            entities=("parser",), supersedes="old", provenance=("review:b",),
        ))
    assert [row["fact_id"] for row in entities_for_project(conn, "repo-a")] == ["old"]
    assert entities_for_project(conn, "repo-b") == []
    assert conn.execute("SELECT COUNT(*) FROM memory_replication_log WHERE project='repo-b'").fetchone()[0] == 0
    conn.close()


def test_supersession_requires_an_indexed_claim_and_acyclic_owned_target(tmp_path):
    with pytest.raises(MemoryReplicationError, match="indexed claim"):
        AuthoritativeFactMetadata(supersedes="missing")
    conn = connect(tmp_path / "memory.db")
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(conn, "first", "repo-a", "first", metadata=AuthoritativeFactMetadata(
        entities=("parser",), provenance=("review:first",),
    ))
    with pytest.raises(MemoryReplicationError, match="same source and project"):
        source.add_fact(conn, "orphan", "repo-a", "orphan", metadata=AuthoritativeFactMetadata(
            entities=("parser",), supersedes="missing", provenance=("review:orphan",),
        ))
    with pytest.raises(MemoryReplicationError, match="cannot supersede itself"):
        source.upsert_fact(conn, "first", "repo-a", "self", metadata=AuthoritativeFactMetadata(
            entities=("parser",), supersedes="first", provenance=("review:self",),
        ))
    source.add_fact(conn, "second", "repo-a", "second", metadata=AuthoritativeFactMetadata(
        entities=("parser",), supersedes="first", provenance=("review:second",),
    ))
    with pytest.raises(MemoryReplicationError, match="silently withdrawn"):
        source.upsert_fact(conn, "second", "repo-a", "forget link", metadata=AuthoritativeFactMetadata(
            entities=("parser",), provenance=("review:second-update",),
        ))
    with pytest.raises(MemoryReplicationError, match="cycle"):
        source.upsert_fact(conn, "first", "repo-a", "cycle", metadata=AuthoritativeFactMetadata(
            entities=("parser",), supersedes="second", provenance=("review:cycle",),
        ))
    assert [row["fact_id"] for row in entities_for_project(conn, "repo-a")] == ["second"]
    assert conn.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 2
    conn.close()
