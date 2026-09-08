import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from sonder_runtime.application.ports.managed_runtime_owner import (
    PreparedManagedOwnerOperation, managed_operation,
)
from sonder_runtime.application.ports.runtime_owner import OwnerRefused
from sonder_runtime.adapters.persistence.runtime_owner import SQLiteManagedRuntimeOwnerJournal


def journal(tmp_path):
    return SQLiteManagedRuntimeOwnerJournal(tmp_path / "owner.sqlite", namespace="namespace", create=True)


def prepare(store, identifier, action="select", payload=None):
    status = store.status()
    return managed_operation(identifier, action, status, payload or {"config": {"generation": 1, "digest": "a" * 64, "selector_revision": 0}})


def test_same_revision_select_has_one_winner_and_exact_original_receipt(tmp_path):
    first = journal(tmp_path)
    second = SQLiteManagedRuntimeOwnerJournal(first.path, namespace="namespace")
    one, two = prepare(first, "one"), prepare(first, "two")
    def admit(pair):
        store, command = pair
        try:
            store.prepare(command)
            return command
        except OwnerRefused:
            return None
    with ThreadPoolExecutor(2) as pool:
        winners = [result for result in pool.map(admit, ((first, one), (second, two))) if result]
    assert len(winners) == 1
    winner = winners[0]
    receipt = first.complete(winner, {"selected": True}, "STOPPED_CLEAN")
    assert second.prepare(winner) == receipt
    assert second.complete(winner, {"selected": False}, "STOPPED_CLEAN") == receipt
    assert first.status()["config_revision"] == 1


def test_pending_launch_and_exact_process_link_survive_reopen(tmp_path):
    store = journal(tmp_path)
    select = prepare(store, "select")
    store.prepare(select)
    store.complete(select, {}, "STOPPED_CLEAN")
    launch = managed_operation("launch", "launch", store.status(), {})
    store.prepare(launch)
    store.phase(launch, "STARTING", {"job_id": "launch", "process_identity": "windows:123", "containment_identity": "job-123"})
    reopened = SQLiteManagedRuntimeOwnerJournal(store.path, namespace="namespace")
    assert reopened.pending() == launch
    assert reopened.status()["state"] == "STARTING"
    assert reopened.phase_evidence(launch, "STARTING")["job_id"] == "launch"
    with pytest.raises(OwnerRefused):
        reopened.prepare(managed_operation("second", "launch", reopened.status(), {}))
    with pytest.raises(OwnerRefused):
        reopened.phase(launch, "STARTING", {"job_id": "other"})


def test_epoch_config_selector_and_operation_shape_are_bound(tmp_path):
    store = journal(tmp_path)
    command = prepare(store, "one")
    store.prepare(command)
    altered = PreparedManagedOwnerOperation(command.operation_id, command.action, command.namespace, command.incarnation, command.expected_revision, command.epoch + 1, command.config_revision, command.selector_revision, command.payload)
    with pytest.raises(OwnerRefused):
        store.prepare(altered)
    with pytest.raises(OwnerRefused):
        store.complete(type("Lookalike", (), dict(operation_id="one"))(), {}, "STOPPED_CLEAN")
    assert store.pending() == command


def test_activation_never_selects_before_durable_complete(tmp_path):
    store = journal(tmp_path)
    select = prepare(store, "select")
    store.prepare(select)
    store.complete(select, {}, "STOPPED_CLEAN")
    activate = managed_operation("cutover", "activate", store.status(), {"manifest_digest": "b" * 64, "target": {"generation": 2, "digest": "c" * 64, "selector_revision": 1}})
    store.prepare(activate)
    store.phase(activate, "ACTIVATION_INCOMPLETE", {"manifest_digest": "b" * 64})
    assert store.status()["config_revision"] == 1
    assert store.selected_config()["digest"] == "a" * 64
    receipt = store.complete(activate, {"phase": "COMPLETE"}, "STOPPED_CLEAN")
    assert receipt["state"] == "STOPPED_CLEAN"
    assert store.selected_config()["digest"] == "c" * 64
    assert store.status()["selector_revision"] == 1


def test_refused_owner_transaction_rolls_back_owned_connection(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence.owned_sqlite import OwnedSQLiteConnections
    import sonder_runtime.adapters.persistence.runtime_owner as module
    owner = OwnedSQLiteConnections((tmp_path,))
    monkeypatch.setattr(module, "owned_sqlite_connect", owner.connect)
    store = journal(tmp_path)
    command = prepare(store, "one")
    store.prepare(command)
    with pytest.raises(OwnerRefused):
        store.prepare(prepare(store, "two"))
    assert owner.snapshot().clean


def test_schema_creation_is_atomic_on_actual_sqlite_failure(tmp_path, monkeypatch):
    import sqlite3
    import sonder_runtime.adapters.persistence.runtime_owner as module
    original = module.owned_sqlite_connect
    def denied(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_authorizer(lambda action, name, *rest:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and name == "managed_command" else sqlite3.SQLITE_OK)
        return connection
    monkeypatch.setattr(module, "owned_sqlite_connect", denied)
    with pytest.raises(OwnerRefused):
        journal(tmp_path)
    connection = original(tmp_path / "owner.sqlite")
    try:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        connection.close()


@pytest.mark.parametrize("namespace", ["namespace", "different"])
def test_same_generation_in_other_namespace_or_incarnation_is_not_authority(tmp_path, namespace):
    first = journal(tmp_path)
    second = SQLiteManagedRuntimeOwnerJournal(tmp_path / "second.sqlite", namespace=namespace, create=True)
    command = prepare(first, "one")
    before = second.status()
    with pytest.raises(OwnerRefused):
        second.prepare(command)
    assert second.status() == before
    assert second.pending() is None
