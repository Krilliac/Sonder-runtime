"""SPEC-2 section 13: consistent, verified, atomically-published backups."""
from __future__ import annotations

import sqlite3

import pytest

from sonder_runtime.adapters import backup as sonder_backup

pytestmark = pytest.mark.unit


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point every authoritative store at this test's directory."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SONDER_DB", str(state / "memory.db"))
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(state / "autopilot.db"))
    monkeypatch.setenv("SONDER_FLEET_DB", str(state / "fleet.db"))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(state / "operations.db"))
    monkeypatch.setenv(
        "SONDER_QUEUED_ACTION_DB", str(state / "queued_actions.db")
    )
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(state / "runtime_policy.json"))
    conn = sqlite3.connect(str(state / "memory.db"))
    conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany(
        "INSERT INTO facts (body) VALUES (?)", [("alpha",), ("beta",)]
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_create_verify_and_list(isolated_state):
    target = isolated_state / "backups"
    result = sonder_backup.create_backup(target)
    assert result.file_count >= 2  # memory.db + operations.db
    assert result.path.is_dir()
    assert not [p for p in target.iterdir() if p.name.startswith(".staging-")]
    assert sonder_backup.verify_backup(result.path) == []
    listed = sonder_backup.list_backups(target)
    assert listed[0]["backup_id"] == result.backup_id


def test_backup_taken_while_db_open_is_consistent(isolated_state, monkeypatch):
    import os

    live = sqlite3.connect(os.environ["SONDER_DB"])
    try:
        live.execute("INSERT INTO facts (body) VALUES ('gamma')")
        live.commit()
        target = isolated_state / "backups"
        result = sonder_backup.create_backup(target)
    finally:
        live.close()
    copy = sqlite3.connect(str(result.path / "state" / "memory.db"))
    try:
        count = copy.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    finally:
        copy.close()
    assert count == 3


def test_backup_includes_migrated_queued_action_ledger(isolated_state):
    import sonder_runtime.adapters.persistence.queued_actions as queued_actions

    conn = queued_actions.connect()
    try:
        queued_actions.propose(conn, queued_actions.ActionRequest.create(
            "backup-action", "inspect", {"target": "local"},
            proposed_by=queued_actions.Actor.USER,
        ))
    finally:
        conn.close()
    result = sonder_backup.create_backup(isolated_state / "backups")
    copied = sqlite3.connect(str(result.path / "state" / "queued_actions.db"))
    try:
        count = copied.execute("SELECT COUNT(*) FROM queued_actions").fetchone()[0]
    finally:
        copied.close()
    assert count == 1


def test_tampered_backup_fails_verification(isolated_state):
    target = isolated_state / "backups"
    result = sonder_backup.create_backup(target)
    victim = result.path / "state" / "memory.db"
    victim.write_bytes(victim.read_bytes() + b"tamper")
    problems = sonder_backup.verify_backup(result.path)
    assert problems
    assert any("memory.db" in p for p in problems)


def test_backup_run_recorded_in_operations(isolated_state):
    from sonder_runtime.adapters.persistence.operations_store import OperationsStore

    target = isolated_state / "backups"
    result = sonder_backup.create_backup(target)
    runs = OperationsStore().list_backup_runs()
    mine = next(r for r in runs if r["backup_id"] == result.backup_id)
    assert mine["status"] == "verified"
    assert mine["total_bytes"] == result.total_bytes


def test_restore_to_empty_and_refuse_nonempty(isolated_state):
    target = isolated_state / "backups"
    result = sonder_backup.create_backup(target)
    dest = isolated_state / "restored"
    restored = sonder_backup.restore_to_empty(result.path, dest)
    assert any(path.endswith("memory.db") for path in restored)
    conn = sqlite3.connect(str(dest / "memory.db"))
    try:
        count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    finally:
        conn.close()
    assert count == 2
    with pytest.raises(sonder_backup.BackupError, match="not empty"):
        sonder_backup.restore_to_empty(result.path, dest)


def test_prune_never_removes_newest_verified(isolated_state):
    target = isolated_state / "backups"
    first = sonder_backup.create_backup(target)
    second = sonder_backup.create_backup(target)
    removed = sonder_backup.prune_backups(target, keep=1)
    remaining = {e["backup_id"] for e in sonder_backup.list_backups(target)}
    assert second.backup_id in remaining
    # A no-op retention bug grows full state copies until the target disk fills.
    assert removed == [str(first.path)]
    assert first.backup_id not in remaining
    with pytest.raises(sonder_backup.BackupError):
        sonder_backup.prune_backups(target, keep=0)
