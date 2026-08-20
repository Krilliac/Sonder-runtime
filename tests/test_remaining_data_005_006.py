from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.epoch_adoption import check_epoch2_cleanup
from sonder_runtime.adapters.persistence.migration_rehearsal import (
    RehearsalError,
    rehearse_bridge_migration,
)
from sonder_runtime.adapters.persistence.sqlite.bridge_migration import run_bridge_migration


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_memory(home):
    db = home / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE interactions (id TEXT PRIMARY KEY, task TEXT, response TEXT, tier TEXT, ts TEXT);
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, detail TEXT, status TEXT,
          priority INTEGER, project TEXT, owner TEXT, parent_id TEXT, created_ts TEXT, updated_ts TEXT);
        CREATE TABLE task_events (id TEXT PRIMARY KEY, task_id TEXT, event TEXT, note TEXT, ts TEXT);
        INSERT INTO interactions VALUES ('i1', 'task', 'answer', 'code', '2026-01-01');
        INSERT INTO tasks VALUES ('t1', 'migrate', 'detail', 'pending', 1, 'p', NULL, NULL, 'now', 'now');
        INSERT INTO task_events VALUES ('e1', 't1', 'created', 'note', 'now');
        """
    )
    conn.commit()
    conn.close()


def test_data_005_rehearsal_proves_backup_restore_and_leaves_source_untouched(tmp_path):
    _legacy_memory(tmp_path)
    before = _digest(tmp_path / "memory.db")

    report = rehearse_bridge_migration(tmp_path)

    assert report.backup_verified
    assert report.restore_verified
    assert report.crash_recovery_verified
    assert report.epoch2_verified
    assert report.cleanup.allowed
    assert _digest(tmp_path / "memory.db") == before
    assert not (tmp_path / "epoch2_adoption_receipt.json").exists()


def test_data_005_requires_a_reachable_fault_boundary(tmp_path):
    with pytest.raises(RehearsalError, match="boundary was not reached"):
        rehearse_bridge_migration(tmp_path, failure_boundary="not-a-boundary")


def test_data_006_cleanup_check_is_read_only_and_rejects_temporary_objects(tmp_path):
    _legacy_memory(tmp_path)
    run_bridge_migration(tmp_path)
    report = check_epoch2_cleanup(tmp_path)
    assert report.allowed

    conn = sqlite3.connect(str(tmp_path / "memory.db"))
    conn.execute("CREATE TABLE tmp_bridge_rows (value TEXT)")
    conn.commit()
    conn.close()
    rejected = check_epoch2_cleanup(tmp_path)
    assert not rejected.allowed
    assert "memory.db" in rejected.temporary_schema_objects
    assert (tmp_path / "tmp_bridge_rows").exists() is False


def test_data_006_rejects_missing_or_wrong_adoption_receipt(tmp_path):
    run_bridge_migration(tmp_path)
    receipt = tmp_path / "epoch2_adoption_receipt.json"
    receipt.write_text(json.dumps({"epoch": 1}), encoding="utf-8")

    report = check_epoch2_cleanup(tmp_path)

    assert not report.allowed
    assert not report.receipt_valid
    assert any("receipt" in reason for reason in report.reasons)
