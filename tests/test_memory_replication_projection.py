from datetime import datetime, timezone
import pytest

from sonder_runtime.adapters.memory_store import connect, facts_for_project, get_interaction
from sonder_runtime.adapters.persistence.sqlite.memory_projection import (
    MemoryProjectionError,
    SQLiteMemoryReplicationProjection,
)
from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    SQLiteMemoryReplicationJournal,
)
from sonder_runtime.domain.memory.replication import (
    MemoryMutation,
    MemoryReplicationBatch,
)


def _mutation(
    *, source_id="node-a", epoch=1, sequence=1, kind="fact", entity_id="fact-1",
    version=1, operation="upsert", project="repo-a", payload=None,
):
    return MemoryMutation(
        source_id=source_id,
        source_epoch=epoch,
        sequence=sequence,
        entity_kind=kind,
        entity_id=entity_id,
        version=version,
        operation=operation,
        project=project,
        payload=payload if payload is not None else {"text": "use pytest"},
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )


def _batch(*records, source_id="node-a", epoch=1, after=0):
    records = tuple(records)
    return MemoryReplicationBatch(
        source_id=source_id,
        source_epoch=epoch,
        after_sequence=after,
        records=records,
        next_sequence=records[-1].sequence if records else after,
        has_more=False,
    )


def test_fact_replay_is_idempotent_and_rebuilds_lexical_store(tmp_path):
    source = SQLiteMemoryReplicationJournal(tmp_path / "source.db", source_id="node-a")
    record = _mutation(payload={"text": "Use pytest", "embedding": [1.0, 0.0]})
    assert source.append((record,)) == 1
    batch = source.export()

    conn = connect(tmp_path / "target.db")
    projection = SQLiteMemoryReplicationProjection(conn)
    assert projection.apply(batch) == 1
    assert projection.apply(batch) == 0
    assert facts_for_project(conn, "repo-a")[0]["text"] == "Use pytest"
    assert projection.current_records(source_id="node-a")[0].digest == record.digest
    conn.close()


def test_tombstone_wins_over_older_value_and_stale_replay_is_rejected(tmp_path):
    source = SQLiteMemoryReplicationJournal(tmp_path / "source.db", source_id="node-a")
    upsert = _mutation(payload={"text": "old value"})
    delete = _mutation(sequence=2, version=2, operation="delete", payload={})
    assert source.append((upsert, delete)) == 2
    conn = connect(tmp_path / "target.db")
    projection = SQLiteMemoryReplicationProjection(conn)
    assert projection.apply(source.export(limit=10)) == 2
    assert facts_for_project(conn, "repo-a") == []
    assert projection.tombstones(source_id="node-a")[0].entity_id == "fact-1"

    stale = _mutation(sequence=3, version=1, payload={"text": "stale"})
    stale_batch = _batch(stale, after=2)
    with pytest.raises(MemoryProjectionError, match="version"):
        projection.apply(stale_batch)
    conn.close()


def test_project_scope_and_sequence_conflicts_fail_closed(tmp_path):
    conn = connect(tmp_path / "target.db")
    projection = SQLiteMemoryReplicationProjection(conn, project_scope="repo-a")
    first = _mutation()
    assert projection.apply(_batch(first)) == 1
    different = _mutation(sequence=2, project="repo-b", entity_id="fact-2")
    with pytest.raises(MemoryProjectionError, match="scope"):
        projection.apply(_batch(different, after=1))
    conflicting = _mutation(sequence=1, payload={"text": "tampered"})
    conflicting_batch = _batch(conflicting)
    with pytest.raises(MemoryProjectionError, match="sequence"):
        projection.apply(conflicting_batch)
    conn.close()


def test_interaction_and_outcome_replay_preserves_project_and_source(tmp_path):
    source = SQLiteMemoryReplicationJournal(tmp_path / "source.db", source_id="node-a")
    interaction = _mutation(
        kind="interaction", entity_id="interaction-1", sequence=1,
        payload={"task": "fix tests", "retrieved_ctx": "", "response": "done", "tier": "code"},
    )
    outcome = _mutation(
        kind="outcome", entity_id="interaction-1:tests_passed", sequence=2,
        payload={"interaction_id": "interaction-1", "signal": "tests_passed", "reward": 1.0, "source": "caller"},
    )
    assert source.append((interaction, outcome)) == 2
    conn = connect(tmp_path / "target.db")
    projection = SQLiteMemoryReplicationProjection(conn)
    assert projection.apply(source.export(limit=10)) == 2
    assert get_interaction(conn, "interaction-1")["project"] == "repo-a"
    row = conn.execute(
        "SELECT signal, reward, source FROM outcomes WHERE interaction_id=?",
        ("interaction-1",),
    ).fetchone()
    assert tuple(row) == ("tests_passed", 1.0, "caller")
    conn.close()


def test_preference_replay_and_rebuild_after_fresh_projection(tmp_path):
    source = SQLiteMemoryReplicationJournal(tmp_path / "source.db", source_id="node-a")
    preference = _mutation(
        kind="preference", entity_id="pref-1", project="global",
        payload={"key": "editor", "text": "use vim", "confidence": 0.9, "evidence_count": 2, "enabled": True},
    )
    assert source.append((preference,)) == 1
    conn = connect(tmp_path / "target.db")
    projection = SQLiteMemoryReplicationProjection(conn)
    assert projection.apply(source.export()) == 1
    conn.close()

    reopened = connect(tmp_path / "target.db")
    fresh = SQLiteMemoryReplicationProjection(reopened)
    assert fresh.rebuild(source_id="node-a") == 1
    row = reopened.execute(
        "SELECT scope, key, text, confidence, evidence_count, enabled FROM preferences WHERE id=?",
        ("pref-1",),
    ).fetchone()
    assert tuple(row) == ("global", "editor", "use vim", 0.9, 2, 1)
    reopened.close()


def test_malformed_payloads_never_mutate_the_store(tmp_path):
    conn = connect(tmp_path / "target.db")
    projection = SQLiteMemoryReplicationProjection(conn)
    bad = _mutation(payload={"text": 12})
    with pytest.raises(MemoryProjectionError, match="text"):
        projection.apply(_batch(bad))
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    conn.close()
