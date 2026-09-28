import hashlib
import sqlite3
from contextlib import contextmanager

import pytest

from sonder_runtime.adapters.execution.durable_output import (
    DurableExecutionOutput, DurableSpillIntegrityError, SQLiteSpillStore,
)
from sonder_runtime.application.ports.artifact_store import SpillSpec, SpillState


def test_durable_spill_round_trips_after_store_reopen(tmp_path):
    path = tmp_path / "output.sqlite"
    store = SQLiteSpillStore(path)
    handle = store.begin(SpillSpec(64, media_type="text/plain"))
    assert handle.write(b"hello") == 5
    artifact = handle.commit()
    handle.close()

    reopened = SQLiteSpillStore(path)
    assert reopened.read(artifact, max_bytes=64) == b"hello"
    snapshot = reopened._snapshot(artifact.artifact_id)
    assert snapshot.state is SpillState.COMMITTED
    assert snapshot.artifact == artifact


def test_execution_output_bridge_binds_digest_size_and_owner(tmp_path):
    output = DurableExecutionOutput(SQLiteSpillStore(tmp_path / "output.sqlite"), max_bytes=64)
    reference = output.spill_text("large output", owner_id="job-1")
    assert reference.digest == hashlib.sha256(b"large output").hexdigest()
    assert reference.owner_id == "job-1"
    assert output.read(reference, max_bytes=64) == b"large output"

    with pytest.raises(ValueError, match="read bound"):
        output.read(reference, max_bytes=1)


def test_digest_or_size_tampering_fails_closed(tmp_path):
    path = tmp_path / "output.sqlite"
    store = SQLiteSpillStore(path)
    output = DurableExecutionOutput(store)
    reference = output.spill_text("immutable", owner_id="job-1")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE execution_spill SET payload=? WHERE digest=?", (b"tampered", reference.digest))
    with pytest.raises(DurableSpillIntegrityError):
        output.read(reference, max_bytes=64)


def test_spill_write_and_output_bounds_fail_without_partial_commit(tmp_path):
    store = SQLiteSpillStore(tmp_path / "output.sqlite")
    handle = store.begin(SpillSpec(3))
    with pytest.raises(ValueError, match="max_bytes"):
        handle.write(b"1234")
    assert handle.snapshot().state is SpillState.OPEN
    handle.abort()
    assert handle.snapshot().state is SpillState.ABORTED

    output = DurableExecutionOutput(store, max_bytes=3)
    with pytest.raises(ValueError, match="spill bound"):
        output.spill_text("1234", owner_id="job-1")


def test_committed_spills_have_owner_and_store_byte_quotas(tmp_path):
    store = SQLiteSpillStore(tmp_path / "output.sqlite", max_owner_bytes=8, max_total_bytes=12)
    output = DurableExecutionOutput(store, max_bytes=8)
    output.spill_text("12345678", owner_id="job-1")
    with pytest.raises(ValueError, match="owner.*quota"):
        output.spill_text("x", owner_id="job-1")
    output.spill_text("1234", owner_id="job-2")
    with pytest.raises(ValueError, match="store.*quota"):
        output.spill_text("x", owner_id="job-3")


def test_reaping_expired_output_references_recovers_spill_quota(tmp_path):
    store = SQLiteSpillStore(tmp_path / "output.sqlite", max_owner_bytes=8, max_total_bytes=8)
    output = DurableExecutionOutput(store, max_bytes=8)
    stale = output.spill_text("12345678", owner_id="job-1")
    assert output.reap_owner("job-1", ()) == 1
    with pytest.raises(DurableSpillIntegrityError):
        output.read(stale, max_bytes=8)
    fresh = output.spill_text("abcdefgh", owner_id="job-2")
    assert output.read(fresh, max_bytes=8) == b"abcdefgh"


def test_reaping_duplicate_spills_keeps_one_blob_per_live_digest(monkeypatch):
    connection = sqlite3.connect(":memory:")

    @contextmanager
    def memory_connection(_store):
        with connection:
            yield connection

    monkeypatch.setattr(SQLiteSpillStore, "_connect", memory_connection)
    try:
        store = SQLiteSpillStore("unused", max_owner_bytes=12, max_total_bytes=12)
        output = DurableExecutionOutput(store, max_bytes=8)
        references = [output.spill_text("1234", owner_id="job-1") for _ in range(3)]

        # Three retained events may share this digest, but one blob backs all of them.
        assert output.reap_owner("job-1", tuple(ref.digest for ref in references)) == 2
        assert all(output.read(ref, max_bytes=8) == b"1234" for ref in references)
        fresh = output.spill_text("abcdefgh", owner_id="job-1")
        assert output.read(fresh, max_bytes=8) == b"abcdefgh"
    finally:
        connection.close()


def test_spill_quota_and_reference_reaping_with_memory_connection(monkeypatch):
    connection = sqlite3.connect(":memory:")

    @contextmanager
    def memory_connection(_store):
        with connection:
            yield connection

    monkeypatch.setattr(SQLiteSpillStore, "_connect", memory_connection)
    try:
        store = SQLiteSpillStore("unused", max_owner_bytes=8, max_total_bytes=12)
        output = DurableExecutionOutput(store, max_bytes=8)
        first = output.spill_text("12345678", owner_id="one")
        with pytest.raises(ValueError, match="owner.*quota"):
            output.spill_text("x", owner_id="one")
        output.spill_text("1234", owner_id="two")
        with pytest.raises(ValueError, match="store.*quota"):
            output.spill_text("x", owner_id="three")
        assert output.reap_owner("one", ()) == 1
        with pytest.raises(DurableSpillIntegrityError):
            output.read(first, max_bytes=8)
        assert output.spill_text("12345678", owner_id="three").size == 8
        with pytest.raises(DurableSpillIntegrityError):
            output.read(first, max_bytes=8)
    finally:
        connection.close()
