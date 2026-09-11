"""Receive-and-project evidence for the narrow fact-only replica sink.

The HTTP handler below is only the existing in-process test adapter.  This
module does not imply that a listener or normal runtime composition exists.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
import json
import sqlite3

import pytest

from sonder_runtime.adapters.memory_store import connect, facts_for_project
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    SQLiteAuthoritativeFactSource,
)
from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    SQLiteFactReplicationSink,
    SQLiteMemoryReplicationJournal,
)
from sonder_runtime.application.memory.replication import MemoryReplicationReceiver
from sonder_runtime.domain.memory.replication import (
    MemoryMutation,
    MemoryReplicaReceipt,
    MemoryReplicationBatch,
    MemoryReplicationError,
)
from sonder_runtime.interfaces.http.memory_replication import (
    handle_memory_replication,
)


def _batch(
    *records: MemoryMutation,
    source_id: str = "node-a",
    source_epoch: int = 1,
    after_sequence: int = 0,
) -> MemoryReplicationBatch:
    return MemoryReplicationBatch(
        source_id=source_id,
        source_epoch=source_epoch,
        after_sequence=after_sequence,
        records=records,
        next_sequence=records[-1].sequence if records else after_sequence,
        has_more=False,
    )


def _fact(
    *,
    sequence: int = 1,
    entity_id: str = "fact-1",
    version: int = 1,
    project: str = "repo-a",
    text: str = "replicated fact",
) -> MemoryMutation:
    return MemoryMutation(
        source_id="node-a",
        source_epoch=1,
        sequence=sequence,
        entity_kind="fact",
        entity_id=entity_id,
        version=version,
        operation="upsert",
        project=project,
        payload={"text": text},
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )


class _Handler:
    """Minimal in-process harness for the existing HTTP framing adapter."""

    def __init__(self, body: bytes, *, before_response=None) -> None:
        self.path = "/v1/memory/replication/batches"
        self.headers = Message()
        self.headers["Authorization"] = "Bearer memory-secret"
        self.headers["Content-Type"] = "application/json"
        self.headers["Content-Length"] = str(len(body))
        self.rfile = BytesIO(body)
        self.responses = []
        self._request_body_consumed = False
        self._before_response = before_response

    def _send_json_payload(self, payload, *, status, headers):
        if self._before_response is not None:
            self._before_response(status, payload)
        self.responses.append((status, payload, headers))


def _wire(batch: MemoryReplicationBatch) -> bytes:
    return json.dumps(
        {"object": "memory_replication_batch", "batch": batch.as_dict()},
        sort_keys=True,
    ).encode("utf-8")


def _receiver(connection):
    return MemoryReplicationReceiver(
        SQLiteFactReplicationSink(
            "node-b", connection, project_scope="repo-a",
        ),
        api_key="memory-secret",
        accepted_source_ids=("node-a",),
    )


def test_fact_receiver_projects_the_normal_target_store_before_emitting_receipt(tmp_path):
    """A matching HTTP receipt is never framed ahead of the normal fact row."""
    source_path = tmp_path / "source.db"
    source_connection = connect(source_path)
    source = SQLiteAuthoritativeFactSource("node-a", project_scope="repo-a")
    source.add_fact(source_connection, "fact-1", "repo-a", "authoritative source")
    source_connection.close()

    source_journal = SQLiteMemoryReplicationJournal(
        source_path, source_id="node-a", project_scope="repo-a",
    )
    target_path = tmp_path / "target.db"
    target = connect(target_path)
    # Keep a second connection open before the receive.  The receipt callback
    # must observe the committed normal fact through this independent reader,
    # never through the writer that applied the batch.
    observer = sqlite3.connect(target_path, isolation_level=None)
    try:
        batch = source_journal.export()
        receiver = _receiver(target)
        assert facts_for_project(target, "repo-a") == []

        def _assert_projection_precedes_receipt(status, payload):
            assert status == 202
            assert payload["object"] == "memory_replication_receipt"
            receipt = MemoryReplicaReceipt.from_dict(payload["receipt"])
            assert receipt.batch_digest == batch.digest
            assert observer.execute(
                "SELECT id,project,text,embedding FROM facts "
                "WHERE project=? ORDER BY id",
                ("repo-a",),
            ).fetchall() == [
                ("fact-1", "repo-a", "authoritative source", None),
            ]

        handler = _Handler(_wire(batch), before_response=_assert_projection_precedes_receipt)
        assert handle_memory_replication(handler, "POST", receiver) is True
        assert handler.responses[0][0] == 202
        assert handler._request_body_consumed is True
    finally:
        observer.close()
        target.close()
        source_journal.close()


@pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "autocommit"),
    reason="Python 3.12 sqlite autocommit mode is unavailable",
)
@pytest.mark.parametrize(
    ("autocommit", "error"),
    ((True, r"autocommit=True"), (False, r"autocommit=False")),
)
def test_fact_projecting_sink_rejects_unsupported_sqlite_autocommit_before_mutation(
    tmp_path, autocommit, error,
):
    """A mode outside the sink's explicit commit contract cannot produce a receipt."""
    target_path = tmp_path / "target.db"
    initialized = connect(target_path)
    initialized.close()
    unsupported = sqlite3.connect(target_path, autocommit=autocommit)
    observer = sqlite3.connect(target_path, isolation_level=None)
    try:
        with pytest.raises(ValueError, match=error):
            SQLiteFactReplicationSink(
                "node-b", unsupported, project_scope="repo-a",
            )

        assert observer.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        assert observer.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'memory_projection_%'"
        ).fetchone()[0] == 0
    finally:
        observer.close()
        unsupported.close()


def test_projection_failure_returns_no_receipt_rolls_back_journal_and_retries_exact_batch(tmp_path):
    """A transient projection failure cannot leave journal-only success evidence."""
    target = connect(tmp_path / "target.db")
    try:
        receiver = _receiver(target)
        batch = _batch(_fact(text="retry after target repair"))
        target.execute(
            "CREATE TRIGGER reject_projected_fact BEFORE INSERT ON facts "
            "BEGIN SELECT RAISE(ABORT, 'projection unavailable'); END"
        )
        target.commit()

        handler = _Handler(_wire(batch))
        assert handle_memory_replication(handler, "POST", receiver) is True
        assert handler.responses == [
            (503, {"error": {"code": "MEMORY_REPLICATION_UNAVAILABLE"}}, {"Cache-Control": "no-store"}),
        ]
        for table in (
            "facts",
            "memory_replication_log",
            "memory_projection_log",
            "memory_projection_state",
            "memory_projection_cursors",
        ):
            assert target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert target.execute(
            "SELECT COUNT(*) FROM memory_replication_meta WHERE source_id=?",
            ("node-a",),
        ).fetchone()[0] == 0

        target.execute("DROP TRIGGER reject_projected_fact")
        target.commit()
        receipt = receiver.receive(
            "Bearer memory-secret",
            {"object": "memory_replication_batch", "batch": batch.as_dict()},
        )

        assert receipt == MemoryReplicaReceipt(
            replica_id="node-b",
            source_id="node-a",
            source_epoch=1,
            next_sequence=1,
            batch_digest=batch.digest,
            durable=True,
            inserted_records=1,
        )
        assert facts_for_project(target, "repo-a")[0]["text"] == "retry after target repair"
    finally:
        target.close()


def test_commit_failure_rolls_back_every_target_state_and_allows_exact_retry(tmp_path):
    """A DELETE-mode reader lock cannot strand an uncommitted received page."""
    target_path = tmp_path / "target.db"
    target = connect(target_path)
    reader = None
    observer = None
    try:
        assert target.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
        target.execute("PRAGMA busy_timeout=0")
        sink = SQLiteFactReplicationSink(
            "node-b", target, project_scope="repo-a",
        )
        batch = _batch(_fact(text="retry after commit lock clears"))

        reader = sqlite3.connect(target_path, isolation_level=None, timeout=0)
        reader.execute("PRAGMA busy_timeout=0")
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM facts").fetchone()

        receipt = None
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            receipt = sink.apply(batch)
        assert receipt is None
        assert target.in_transaction is False

        observer = sqlite3.connect(target_path, isolation_level=None, timeout=0)
        for table in (
            "facts",
            "memory_replication_log",
            "memory_replication_meta",
            "memory_projection_log",
            "memory_projection_state",
            "memory_projection_cursors",
        ):
            assert observer.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        observer.close()
        observer = None

        reader.execute("ROLLBACK")
        reader.close()
        reader = None

        receipt = sink.apply(batch)
        assert receipt.durable is True
        assert receipt.inserted_records == 1
        assert facts_for_project(target, "repo-a") == [
            {
                "id": "fact-1",
                "project": "repo-a",
                "text": "retry after commit lock clears",
                "embedding": None,
            }
        ]
    finally:
        if observer is not None:
            observer.close()
        if reader is not None:
            reader.close()
        target.close()


def test_fact_projecting_sink_rejects_other_kinds_scopes_and_oversized_pages_before_mutation(tmp_path):
    target = connect(tmp_path / "target.db")
    try:
        sink = SQLiteFactReplicationSink(
            "node-b", target, project_scope="repo-a", max_records=1,
        )
        interaction = MemoryMutation(
            source_id="node-a",
            source_epoch=1,
            sequence=1,
            entity_kind="interaction",
            entity_id="interaction-1",
            version=1,
            operation="upsert",
            project="repo-a",
            payload={"task": "task", "retrieved_ctx": "", "response": "done", "tier": "code"},
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
        too_many = _batch(
            _fact(sequence=1, entity_id="fact-1"),
            _fact(sequence=2, entity_id="fact-2"),
        )

        with pytest.raises(MemoryReplicationError, match="fact"):
            sink.apply(_batch(interaction))
        with pytest.raises(MemoryReplicationError, match="scope"):
            sink.apply(_batch(_fact(project="repo-b")))
        with pytest.raises(MemoryReplicationError, match="bound"):
            sink.apply(too_many)
        assert target.execute("SELECT COUNT(*) FROM memory_replication_log").fetchone()[0] == 0
        assert target.execute("SELECT COUNT(*) FROM memory_projection_log").fetchone()[0] == 0
        assert facts_for_project(target, "repo-a") == []
    finally:
        target.close()
