"""Private immutable terminal text is durably scoped and bounded."""

from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.lane_continuation import ProjectionBinding


@pytest.fixture
def data(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    context = local_owner_context(
        correlation_id="output", workspace_roots=(project,), timeout_seconds=60
    )
    binding = ProjectionBinding(
        "continuation",
        context.principal_id,
        "run",
        "host",
        "parent",
        1,
        "verification",
        "b" * 64,
        (str(project),),
        1,
    )
    return tmp_path / "private-output", project, context, binding


def store_for(data, **kwargs):
    from sonder_runtime.adapters.persistence.terminal_output import (
        SQLiteTerminalOutputStore,
    )

    root, project, context, binding = data
    return SQLiteTerminalOutputStore(
        root, model_writable_roots=lambda: (project,), **kwargs
    )


def test_large_unicode_output_reopens_exactly_and_retry_is_immutable(data):
    store = store_for(data)
    _, _, context, binding = data
    output = "original terminal output α\n" * 4000
    reference = store.put(binding, output, context=context)
    assert reference.size_bytes == len(output.encode()) > 16384
    assert reference.sha256 == hashlib.sha256(output.encode()).hexdigest()
    assert store.put(binding, output, context=context) == reference
    reopened = store_for(data)
    assert reopened.get(binding, reference, context=context) == output
    with pytest.raises(ValueError):
        reopened.put(binding, "replacement", context=context)


def test_foreign_binding_principal_and_reference_cannot_read(data):
    store = store_for(data)
    _, _, context, binding = data
    reference = store.put(binding, "private", context=context)
    with pytest.raises(PermissionError):
        store.get(replace(binding, principal_id="foreign"), reference, context=context)
    with pytest.raises((KeyError, PermissionError)):
        store.get(
            replace(binding, continuation_id="foreign"), reference, context=context
        )
    with pytest.raises((ValueError, PermissionError)):
        store.get(binding, replace(reference, sha256="c" * 64), context=context)


def test_blob_and_aggregate_and_row_bounds(data):
    store = store_for(data, max_blob_bytes=32, max_total_bytes=1500, max_rows=1)
    _, _, context, binding = data
    with pytest.raises(ValueError):
        store.put(binding, "x" * 33, context=context)
    first = store.put(binding, "x" * 32, context=context)
    with pytest.raises(ValueError):
        store.put(replace(binding, run_id="other"), "y", context=context)
    assert store.get(binding, first, context=context) == "x" * 32
    other = replace(binding, run_id="other")
    store = store_for(data, max_total_bytes=1)
    with pytest.raises(ValueError):
        store.put(other, "y", context=context)


def test_live_root_expansion_refuses_io_without_mutation(data):
    store = store_for(data)
    root, project, context, binding = data
    reference = store.put(binding, "original", context=context)
    before = (root / "terminal-output.sqlite").read_bytes()
    store.model_writable_roots = lambda: (project, root.parent)
    with pytest.raises(PermissionError):
        store.get(binding, reference, context=context)
    with pytest.raises(PermissionError):
        store.put(replace(binding, run_id="other"), "changed", context=context)
    assert (root / "terminal-output.sqlite").read_bytes() == before


def test_corrupt_persisted_bytes_never_replay_a_durable_reference(data):
    store = store_for(data)
    root, _, context, binding = data
    reference = store.put(binding, "original", context=context)
    with sqlite3.connect(root / "terminal-output.sqlite") as conn:
        conn.execute("UPDATE terminal_outputs SET payload=?", (b"corrupt",))
    with pytest.raises(ValueError):
        store.get(binding, reference, context=context)
    with pytest.raises(ValueError):
        store.put(binding, "original", context=context)


def test_missing_root_inventory_and_context_overlap_fail_before_creation(data):
    from sonder_runtime.adapters.persistence.terminal_output import (
        SQLiteTerminalOutputStore,
    )

    root, project, context, binding = data
    with pytest.raises(PermissionError):
        SQLiteTerminalOutputStore(root)
    assert not root.exists()
    store = store_for(data)
    with pytest.raises(PermissionError):
        store.put(
            binding, "private", context=replace(context, workspace_roots=(root.parent,))
        )


def test_concurrent_stores_share_atomic_row_quota(data):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    first, second = store_for(data, max_rows=1), store_for(data, max_rows=1)
    _, _, context, binding = data
    barrier = threading.Barrier(2)

    def put(store, name):
        barrier.wait()
        try:
            return store.put(replace(binding, run_id=name), name, context=context)
        except ValueError:
            return None

    with ThreadPoolExecutor(2) as executor:
        futures = [
            executor.submit(put, first, "first"),
            executor.submit(put, second, "second"),
        ]
        results = [future.result() for future in futures]
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_hardlinked_database_and_sidecars_refused(data, suffix):
    import os

    store = store_for(data)
    root, _, context, binding = data
    reference = store.put(binding, "original", context=context)
    path = root / ("terminal-output.sqlite" + suffix)
    if suffix:
        path.write_bytes(b"sidecar")
    alias = root.parent / "alias"
    os.link(path, alias)
    before = path.read_bytes()
    with pytest.raises(PermissionError):
        store.get(binding, reference, context=context)
    assert path.read_bytes() == before


def test_root_expands_before_commit_rolls_back_without_receipt(data, monkeypatch):
    store = store_for(data)
    root, project, context, binding = data
    reference = store.put(binding, "original", context=context)
    original = store._check
    checks = 0

    def check(binding, context):
        nonlocal checks
        checks += 1
        if checks == 3:
            store.model_writable_roots = lambda: (root.parent,)
        original(binding, context)

    monkeypatch.setattr(store, "_check", check)
    with pytest.raises(PermissionError):
        store.put(replace(binding, run_id="new"), "rejected", context=context)
    store.model_writable_roots = lambda: (project,)
    monkeypatch.setattr(store, "_check", original)
    assert store.get(binding, reference, context=context) == "original"
    with sqlite3.connect(root / "terminal-output.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM terminal_outputs").fetchone() == (1,)


def test_full_commit_then_lost_response_replays_exact_bytes(data, monkeypatch):
    import sonder_runtime.adapters.persistence.terminal_output as module

    store = store_for(data)
    root, _, context, binding = data
    connect = sqlite3.connect

    class LostResponse(sqlite3.Connection):
        def commit(self):
            super().commit()
            raise OSError("simulated commit response loss")

    monkeypatch.setattr(
        module.sqlite3,
        "connect",
        lambda *args, **kwargs: connect(*args, **kwargs, factory=LostResponse),
    )
    with pytest.raises(OSError):
        store.put(binding, "committed", context=context)
    monkeypatch.setattr(module.sqlite3, "connect", connect)
    reopened = store_for(data)
    reference = reopened.put(binding, "committed", context=context)
    assert reopened.get(binding, reference, context=context) == "committed"
    with connect(root / "terminal-output.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM terminal_outputs").fetchone() == (1,)
