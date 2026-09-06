"""Coordinator evidence for bounded authoritative memory replication."""

from datetime import datetime, timezone
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    SQLiteMemoryReplicationJournal,
)
from sonder_runtime.application.memory.replication import (
    MemoryReplicationCoordinator,
    SQLiteMemoryReplicationSink,
)
from sonder_runtime.domain.memory.replication import (
    MemoryMutation,
    MemoryReplicationBatch,
    MemoryReplicationError,
    MemoryReplicaReceipt,
)


def _record(*, source_id="source-a", sequence=1, project="project-a"):
    return MemoryMutation(
        source_id=source_id,
        source_epoch=1,
        sequence=sequence,
        entity_kind="fact",
        entity_id=f"fact-{sequence}",
        version=sequence,
        operation="upsert",
        project=project,
        payload={"text": f"fact {sequence}"},
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )


def _source(tmp_path):
    journal = SQLiteMemoryReplicationJournal(tmp_path / "source.sqlite", source_id="source-a")
    journal.append((_record(),))
    return journal


def test_batch_wire_round_trip_binds_digest_and_rejects_tampering():
    batch = MemoryReplicationBatch("source-a", 1, 0, (_record(),), 1, False)

    decoded = MemoryReplicationBatch.from_dict(batch.as_dict())

    assert decoded == batch
    assert decoded.digest == batch.digest
    tampered = batch.as_dict()
    tampered["records"][0]["payload"]["text"] = "changed"
    with pytest.raises(MemoryReplicationError, match="digest"):
        MemoryReplicationBatch.from_dict(tampered)


def test_coordinator_requires_durable_replica_receipts(tmp_path):
    source = _source(tmp_path)
    first = SQLiteMemoryReplicationJournal(tmp_path / "first.sqlite", source_id="first")
    second = SQLiteMemoryReplicationJournal(tmp_path / "second.sqlite", source_id="second")
    coordinator = MemoryReplicationCoordinator(
        source,
        (SQLiteMemoryReplicationSink("first", first), SQLiteMemoryReplicationSink("second", second)),
        minimum_data_replicas=2,
    )

    result = coordinator.replicate()

    assert result.status == "replicated"
    assert result.replica_ids == ("source-a", "first", "second")
    for path in (first.path, second.path):
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM memory_replication_log WHERE source_id=?",
                ("source-a",),
            ).fetchone()[0] == 1


def test_single_host_profile_counts_only_the_authoritative_source(tmp_path):
    source = _source(tmp_path)

    result = MemoryReplicationCoordinator(
        source, (), minimum_data_replicas=1
    ).replicate()

    assert result.status == "replicated"
    assert result.replica_ids == ("source-a",)
    assert result.failed_replica_ids == ()


def test_coordinator_reports_pending_when_a_sink_fails(tmp_path):
    source = _source(tmp_path)
    first = SQLiteMemoryReplicationJournal(tmp_path / "first.sqlite", source_id="first")

    class FailingSink:
        identity = "unavailable"

        def apply(self, batch):
            raise OSError("peer unavailable")

    coordinator = MemoryReplicationCoordinator(
        source,
        (SQLiteMemoryReplicationSink("first", first), FailingSink()),
        minimum_data_replicas=3,
    )

    result = coordinator.replicate()

    assert result.status == "pending"
    assert result.replica_ids == ("source-a", "first")
    assert result.failed_replica_ids == ("unavailable",)


def test_invalid_receipt_cannot_satisfy_replica_quorum(tmp_path):
    source = _source(tmp_path)

    class WrongReceipt:
        identity = "wrong"

        def apply(self, batch):
            return MemoryReplicaReceipt(
                replica_id="wrong",
                source_id=batch.source_id,
                source_epoch=batch.source_epoch,
                next_sequence=batch.next_sequence,
                batch_digest="0" * 64,
                durable=True,
            )

    result = MemoryReplicationCoordinator(source, (WrongReceipt(),)).replicate()

    assert result.status == "pending"
    assert result.replica_ids == ("source-a",)
    assert result.failed_replica_ids == ("wrong",)
    assert result.failure_reasons == (("wrong", "receipt_digest_mismatch"),)


def test_invalid_inserted_count_cannot_be_reported_as_durable(tmp_path):
    source = _source(tmp_path)

    class InflatedReceipt:
        identity = "inflated"

        def apply(self, batch):
            return MemoryReplicaReceipt(
                replica_id="inflated",
                source_id=batch.source_id,
                source_epoch=batch.source_epoch,
                next_sequence=batch.next_sequence,
                batch_digest=batch.digest,
                durable=True,
                inserted_records=2,
            )

    result = MemoryReplicationCoordinator(
        source, (InflatedReceipt(),), minimum_data_replicas=2
    ).replicate()

    assert result.status == "pending"
    assert result.replica_ids == ("source-a",)
    assert result.failure_reasons == (("inflated", "receipt_inserted_count_mismatch"),)


def test_retry_is_idempotent_and_empty_export_is_not_a_false_ack(tmp_path):
    source = _source(tmp_path)
    target = SQLiteMemoryReplicationJournal(tmp_path / "target.sqlite", source_id="target")
    coordinator = MemoryReplicationCoordinator(
        source, (SQLiteMemoryReplicationSink("target", target),)
    )

    first = coordinator.replicate()
    retry = coordinator.replicate()
    empty = coordinator.replicate(after_sequence=1)

    assert first.status == retry.status == "replicated"
    assert retry.inserted_records == 0
    assert empty.status == "empty"
    assert empty.replica_ids == ("source-a",)
