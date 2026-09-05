from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from sonder_runtime.application.ports.runtime_owner import (
    prepare_owner_operation,
    OwnerRefused,
)
from sonder_runtime.adapters.persistence.runtime_owner import SQLiteRuntimeOwnerJournal


def test_cross_connection_admission_has_one_winner_and_replay_is_original(tmp_path):
    path = tmp_path / "owner.db"
    first = SQLiteRuntimeOwnerJournal(path, namespace="fixture", create=True)
    second = SQLiteRuntimeOwnerJournal(path, namespace="fixture")
    barrier = Barrier(2)

    def admit(pair):
        store, identity = pair
        command = prepare_owner_operation(
            identity, "select", 0, {"config": {"port": 12345}}
        )
        barrier.wait()
        try:
            store.prepare(command)
            return command
        except OwnerRefused:
            return None

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(admit, [(first, "a"), (second, "b")]))
    winner = next(item for item in results if item is not None)
    assert sum(item is not None for item in results) == 1
    assert first.status()["pending"] == winner.operation_id
    receipt = first.complete(winner, {"selected": True}, "STOPPED_CLEAN")
    assert second.prepare(winner) == receipt
    assert second.complete(winner, {"different": True}, "STOPPED_CLEAN") == receipt
    with pytest.raises(OwnerRefused):
        second.prepare(
            prepare_owner_operation(
                winner.operation_id, "select", 0, {"config": {"port": 54321}}
            )
        )


def test_pending_command_survives_reopen_and_unknown_namespace_refused(tmp_path):
    path = tmp_path / "owner.db"
    store = SQLiteRuntimeOwnerJournal(path, namespace="fixture", create=True)
    args = {"config": {"port": 12345}}
    prepared = prepare_owner_operation("exact-id", "select", 0, args)
    args["config"]["port"] = 54321
    store.prepare(prepared)
    reopened = SQLiteRuntimeOwnerJournal(path, namespace="fixture")
    assert reopened.pending() == prepared
    with pytest.raises(OwnerRefused):
        reopened.prepare(prepare_owner_operation("other", "launch", 1, {}))
    reopened.complete(prepared, {"selected": True}, "STOPPED_CLEAN")
    assert reopened.selected_config() == {"port": 12345}
    with pytest.raises(OwnerRefused):
        SQLiteRuntimeOwnerJournal(path, namespace="unknown")


def test_running_or_unclean_owner_never_admits_new_launch(tmp_path):
    store = SQLiteRuntimeOwnerJournal(
        tmp_path / "owner.db", namespace="fixture", create=True
    )
    select = prepare_owner_operation("select", "select", 0, {"config": {"port": 12345}})
    store.prepare(select)
    store.complete(select, {}, "STOPPED_CLEAN")
    launch = prepare_owner_operation("launch", "launch", 2, {})
    store.prepare(launch)
    store.complete(launch, {}, "RUNNING")
    with pytest.raises(OwnerRefused):
        store.prepare(prepare_owner_operation("again", "launch", 4, {}))
    stop = prepare_owner_operation("stop", "stop", 4, {})
    store.prepare(stop)
    store.complete(stop, {}, "STOPPED_UNCLEAN")
    with pytest.raises(OwnerRefused):
        store.prepare(prepare_owner_operation("unsafe", "launch", 6, {}))


def test_process_crash_retains_exact_prepared_identity(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "owner.db"
    SQLiteRuntimeOwnerJournal(path, namespace="fixture", create=True)
    script = """
import os,sys
from sonder_runtime.application.ports.runtime_owner import prepare_owner_operation
from sonder_runtime.adapters.persistence.runtime_owner import SQLiteRuntimeOwnerJournal
store=SQLiteRuntimeOwnerJournal(sys.argv[1],namespace='fixture')
store.prepare(prepare_owner_operation('crashed','select',0,{'config':{'port':12345}}))
os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], timeout=15, capture_output=True
    )
    assert result.returncode == 17
    reopened = SQLiteRuntimeOwnerJournal(path, namespace="fixture")
    pending = reopened.pending()
    assert pending.operation_id == "crashed"
    with pytest.raises(OwnerRefused):
        reopened.prepare(prepare_owner_operation("new", "launch", 1, {}))
    reopened.complete(pending, {"reconciled": True}, "STOPPED_CLEAN")
    assert reopened.selected_config() == {"port": 12345}


def test_receipt_capacity_preserves_history_and_config_is_checked(tmp_path):
    import sqlite3

    path = tmp_path / "owner.db"
    store = SQLiteRuntimeOwnerJournal(path, namespace="fixture", create=True)
    store.MAX_OPERATIONS = 1
    select = prepare_owner_operation("select", "select", 0, {"config": {"port": 12345}})
    store.prepare(select)
    receipt = store.complete(select, {}, "STOPPED_CLEAN")
    with pytest.raises(OwnerRefused, match="capacity"):
        store.prepare(prepare_owner_operation("launch", "launch", 2, {}))
    assert store.prepare(select) == receipt
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE config SET payload=?", (b"{}",))
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(OwnerRefused, match="configuration changed"):
        store.selected_config()


def test_lost_commit_response_retains_original_receipt_and_exact_command(
    tmp_path, monkeypatch
):
    import sqlite3
    from sonder_runtime.application.ports.runtime_owner import OwnerCommitAmbiguous

    store = SQLiteRuntimeOwnerJournal(
        tmp_path / "owner.db", namespace="fixture", create=True
    )
    command = prepare_owner_operation("same", "select", 0, {"config": {"port": 12345}})
    store.prepare(command)
    connect = sqlite3.connect

    class LostResponse(sqlite3.Connection):
        def commit(self):
            super().commit()
            raise sqlite3.OperationalError("fixture response lost after real commit")

    with monkeypatch.context() as patch:
        patch.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: connect(*args, factory=LostResponse, **kwargs),
        )
        with pytest.raises(OwnerCommitAmbiguous) as error:
            store.complete(command, {"original": True}, "STOPPED_CLEAN")
        assert error.value.prepared == command
    replay = store.complete(command, {"replacement": True}, "STOPPED_CLEAN")
    assert replay["result"] == {"original": True}
    assert store.status()["revision"] == 2
