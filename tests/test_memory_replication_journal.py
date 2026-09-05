from datetime import datetime, timezone

import pytest

from sonder_runtime.domain.memory.replication import (
    MemoryMutation,
    MemoryReplicationError,
)
from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    SQLiteMemoryReplicationJournal,
)


NOW = "2026-09-05T12:00:00+00:00"


def _mutation(
    sequence=1,
    *,
    entity_kind="fact",
    entity_id="fact-1",
    version=1,
    operation="upsert",
    project="sonder",
    payload=None,
    source_epoch=1,
    recorded_at=NOW,
):
    return MemoryMutation(
        source_id="cluster-a",
        source_epoch=source_epoch,
        sequence=sequence,
        entity_kind=entity_kind,
        entity_id=entity_id,
        version=version,
        operation=operation,
        project=project,
        payload=payload if payload is not None else {"text": "keep scope"},
        recorded_at=recorded_at,
    )


def test_mutation_digest_and_tombstone_are_bounded_and_canonical():
    mutation = _mutation()
    assert len(mutation.digest) == 64
    tombstone = _mutation(
        operation="delete", payload={}, version=2, sequence=2,
    )
    assert tombstone.is_tombstone
    assert tombstone.payload == {}
    with pytest.raises(MemoryReplicationError, match="payload"):
        _mutation(operation="delete", payload={"text": "must not retain"})
    with pytest.raises(MemoryReplicationError, match="timestamp"):
        _mutation(recorded_at="2026-09-05T12:00:00")


def test_append_export_and_replay_are_idempotent_and_bounded(tmp_path):
    source = SQLiteMemoryReplicationJournal(tmp_path / "source.db")
    target = SQLiteMemoryReplicationJournal(tmp_path / "target.db")
    records = (
        _mutation(sequence=1),
        _mutation(
            sequence=2, entity_id="fact-2", version=1,
            payload={"text": "second"},
        ),
    )
    assert source.append(records) == 2
    batch = source.export(after_sequence=0, limit=1)
    assert batch.records == (records[0],)
    assert batch.has_more is True
    assert batch.next_sequence == 1
    assert target.apply(batch) == 1
    assert target.apply(batch) == 0
    second = source.export(after_sequence=batch.next_sequence, limit=4)
    assert target.apply(second) == 1
    assert [item.entity_id for item in target.current_records()] == ["fact-1", "fact-2"]


def test_replay_rejects_same_sequence_or_entity_version_conflicts(tmp_path):
    journal = SQLiteMemoryReplicationJournal(tmp_path / "memory.db")
    journal.append((_mutation(),))
    with pytest.raises(MemoryReplicationError, match="sequence"):
        journal.append((_mutation(payload={"text": "different"}),))
    with pytest.raises(MemoryReplicationError, match="version"):
        journal.append((_mutation(sequence=2, payload={"text": "new"}, version=1),))


def test_tombstones_survive_rebuild_and_project_scope_is_never_widened(tmp_path):
    journal = SQLiteMemoryReplicationJournal(tmp_path / "memory.db", project_scope="sonder")
    journal.append((_mutation(), _mutation(
        sequence=2, version=2, operation="delete", payload={},
    )))
    assert journal.current_records(project="sonder") == ()
    assert journal.tombstones(project="sonder")[0].entity_id == "fact-1"
    assert journal.current_records(project=None) == ()
    with pytest.raises(MemoryReplicationError, match="project"):
        journal.export(project="other")


def test_delete_must_advance_version_and_retention_is_explicit(tmp_path):
    journal = SQLiteMemoryReplicationJournal(tmp_path / "memory.db")
    journal.append((_mutation(),))
    with pytest.raises(MemoryReplicationError, match="advance"):
        journal.append((_mutation(sequence=2, operation="delete", payload={}),))
    assert journal.append((_mutation(
        sequence=2, operation="delete", payload={}, version=2,
    ),)) == 1
    assert journal.prune_before(2) == 1
    assert journal.tombstones()[0].entity_id == "fact-1"
    assert journal.prune_before(3, retain_tombstones=False) == 1
    assert journal.tombstones() == ()


def test_epoch_regression_is_rejected_and_export_cursor_is_bounded(tmp_path):
    journal = SQLiteMemoryReplicationJournal(tmp_path / "memory.db")
    journal.append((_mutation(),))
    with pytest.raises(MemoryReplicationError, match="epoch"):
        journal.append((_mutation(sequence=2, source_epoch=0)))
    with pytest.raises(ValueError, match="limit"):
        journal.export(limit=0)
    with pytest.raises(ValueError, match="sequence"):
        journal.export(after_sequence=-1)


def test_unscoped_project_filter_is_rejected_to_preserve_cursor_continuity(tmp_path):
    journal = SQLiteMemoryReplicationJournal(tmp_path / "memory.db")
    journal.append((_mutation(),))
    with pytest.raises(MemoryReplicationError, match="project-scoped"):
        journal.export(project="sonder")
