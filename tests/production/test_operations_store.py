"""SPEC-2 section 8: durable, redacted operations events and locks."""
from __future__ import annotations

import json
import sqlite3

import pytest

from sonder_logging import Redactor
from sonder_runtime.adapters.persistence.operations_store import MaintenanceLockHeld, OperationsStore

pytestmark = pytest.mark.unit


@pytest.fixture()
def store(tmp_path):
    return OperationsStore(
        str(tmp_path / "operations.db"),
        redactor=Redactor(env={"SONDER_API_KEY": "secret-op-key-4321"}),
    )


def test_event_roundtrip(store):
    event = store.record_event(
        component="http",
        event_code="AUTH_FAILED",
        severity="WARNING",
        summary="authentication failed",
        detail={"reason": "bad-key", "attempts": 3},
        correlation_id="req_1",
        principal_id="owner",
    )
    events = store.recent_events()
    assert any(e.event_id == event.event_id for e in events)
    found = next(e for e in events if e.event_id == event.event_id)
    assert found.event_code == "AUTH_FAILED"
    assert found.detail["attempts"] == 3


def test_event_detail_is_redacted(store, tmp_path):
    store.record_event(
        component="http",
        event_code="AUTH_FAILED",
        summary="key secret-op-key-4321 rejected",
        detail={"presented": "secret-op-key-4321"},
    )
    conn = sqlite3.connect(str(tmp_path / "operations.db"))
    try:
        rows = conn.execute(
            "SELECT summary, detail_json FROM operation_event"
        ).fetchall()
    finally:
        conn.close()
    dumped = json.dumps(rows)
    assert "secret-op-key-4321" not in dumped


def test_invalid_severity_rejected(store):
    with pytest.raises(ValueError):
        store.record_event(
            component="x", event_code="Y", severity="LOUD", summary="s"
        )


def test_maintenance_lock_conflict_and_release(store):
    store.acquire_maintenance_lock(
        "backup", owner_id="proc-1", reason="backup running"
    )
    with pytest.raises(MaintenanceLockHeld):
        store.acquire_maintenance_lock(
            "backup", owner_id="proc-2", reason="second backup"
        )
    assert store.release_maintenance_lock("backup", owner_id="proc-2") is False
    assert store.release_maintenance_lock("backup", owner_id="proc-1") is True
    store.acquire_maintenance_lock(
        "backup", owner_id="proc-2", reason="now free"
    )


def test_expired_maintenance_lock_reclaimed(store, tmp_path):
    store.acquire_maintenance_lock(
        "update", owner_id="proc-1", reason="stale", ttl_seconds=1
    )
    # Simulate expiry by rewriting the expiry timestamp into the past.
    conn = sqlite3.connect(str(tmp_path / "operations.db"))
    try:
        conn.execute(
            "UPDATE maintenance_lock SET expires_at_utc = '2000-01-01T00:00:00Z'"
        )
        conn.commit()
    finally:
        conn.close()
    store.acquire_maintenance_lock(
        "update", owner_id="proc-2", reason="reclaim"
    )
    locks = store.active_maintenance_locks()
    assert [l["owner_id"] for l in locks if l["lock_name"] == "update"] == [
        "proc-2"
    ]


def test_backup_run_lifecycle(store):
    backup_id = store.start_backup_run("0.9.0")
    store.finish_backup_run(
        backup_id, status="verified", manifest_path="/x/manifest.json",
        total_bytes=1234,
    )
    runs = store.list_backup_runs()
    assert runs[0]["backup_id"] == backup_id
    assert runs[0]["status"] == "verified"
    assert runs[0]["total_bytes"] == 1234
    with pytest.raises(ValueError):
        store.finish_backup_run("bkp_missing", status="failed")
    with pytest.raises(ValueError):
        store.finish_backup_run(backup_id, status="running")


def test_prune_events(store):
    store.record_event(component="x", event_code="OLD", summary="old")
    # Nothing is older than 1 day yet.
    assert store.prune_events(retention_days=1) == 0
