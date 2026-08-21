import hashlib
import sqlite3

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
