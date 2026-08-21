"""SPEC-5 WP2: Bridge migration and schema epoch tests."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
    EPOCH,
    check_epoch,
    require_epoch_2,
    run_bridge_migration,
)
from sonder_runtime.domain.common.errors import MigrationRequired


@pytest.fixture
def sonder_home(tmp_path):
    return tmp_path


def _create_legacy_memory_db(sonder_home: Path) -> None:
    """Create a minimal epoch-1 memory.db with tasks."""
    db = sonder_home / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""\
        CREATE TABLE interactions (
            id TEXT PRIMARY KEY, task TEXT, response TEXT, tier TEXT, ts TEXT
        );
        CREATE TABLE outcomes (
            interaction_id TEXT, signal TEXT, reward REAL, ts TEXT
        );
        CREATE TABLE lessons (id TEXT PRIMARY KEY, text TEXT, ts TEXT);
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE facts (id TEXT PRIMARY KEY, text TEXT, ts TEXT);
        CREATE TABLE preferences (
            id TEXT PRIMARY KEY, scope TEXT, key TEXT, text TEXT,
            enabled INTEGER DEFAULT 1, revision INTEGER DEFAULT 0,
            created_ts TEXT, updated_ts TEXT
        );
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, detail TEXT, status TEXT,
            priority INTEGER, project TEXT, owner TEXT, parent_id TEXT,
            created_ts TEXT, updated_ts TEXT
        );
        CREATE TABLE task_events (
            id TEXT PRIMARY KEY, task_id TEXT, event TEXT, note TEXT, ts TEXT
        );

        INSERT INTO interactions VALUES ('i1', 'test task', 'response', 'code', '2026-01-01');
        INSERT INTO tasks VALUES ('t1', 'Fix bug', 'Details', 'pending', 1, 'proj', NULL, NULL, '2026-01-01', '2026-01-01');
        INSERT INTO tasks VALUES ('t2', 'Add feature', 'More details', 'done', 2, 'proj', NULL, NULL, '2026-01-01', '2026-01-02');
        INSERT INTO task_events VALUES ('e1', 't1', 'created', 'note1', '2026-01-01');
    """)
    conn.commit()
    conn.close()


class TestEpochCheck:
    def test_no_db_returns_none(self, sonder_home):
        assert check_epoch(sonder_home / "nonexistent.db") is None

    def test_no_epoch_table_returns_none(self, sonder_home):
        db = sonder_home / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE foo (x TEXT)")
        conn.commit()
        conn.close()
        assert check_epoch(db) is None

    def test_epoch_2_detected(self, sonder_home):
        db = sonder_home / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""\
            CREATE TABLE schema_epoch (
                epoch INTEGER, completed_at TEXT, source_version TEXT
            )
        """)
        conn.execute(
            "INSERT INTO schema_epoch VALUES (2, '2026-01-01', 'test')"
        )
        conn.commit()
        conn.close()
        assert check_epoch(db) == 2


class TestRequireEpoch2:
    def test_fresh_install_succeeds(self, sonder_home):
        require_epoch_2(sonder_home)  # No memory.db → OK

    def test_pre_epoch_fails(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        with pytest.raises(MigrationRequired):
            require_epoch_2(sonder_home)

    def test_epoch_2_succeeds(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        run_bridge_migration(sonder_home)
        require_epoch_2(sonder_home)  # Should not raise

    def test_partial_epoch_2_adoption_fails_closed(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        run_bridge_migration(sonder_home)
        (sonder_home / "training.db").unlink()
        with pytest.raises(MigrationRequired, match="training.db"):
            require_epoch_2(sonder_home)


class TestBridgeMigration:
    def test_new_install_creates_epoch_2(self, sonder_home):
        receipt = run_bridge_migration(sonder_home)
        assert receipt.epoch == EPOCH
        assert receipt.tasks_migrated == 0

    def test_tasks_migrated_to_automation(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        receipt = run_bridge_migration(sonder_home)
        assert receipt.tasks_migrated == 2

        automation = sqlite3.connect(str(sonder_home / "automation.db"))
        runs = automation.execute(
            "SELECT id, kind, objective FROM automation_runs ORDER BY id"
        ).fetchall()
        assert len(runs) == 2
        assert runs[0][1] == "task"
        assert runs[0][2] == "Fix bug"
        automation.close()

    def test_task_events_migrated(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        run_bridge_migration(sonder_home)

        automation = sqlite3.connect(str(sonder_home / "automation.db"))
        events = automation.execute(
            "SELECT run_id, event_type FROM task_events"
        ).fetchall()
        assert len(events) == 1
        assert events[0][0] == "t1"
        automation.close()

    def test_backup_created(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        receipt = run_bridge_migration(sonder_home)
        backup_dir = Path(receipt.backup_path)
        assert backup_dir.exists()
        assert (backup_dir / "memory.db").exists()

    def test_adoption_receipt_written(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        run_bridge_migration(sonder_home)
        receipt_path = sonder_home / "epoch2_adoption_receipt.json"
        assert receipt_path.exists()
        data = json.loads(receipt_path.read_text())
        assert data["epoch"] == 2
        assert data["tasks_migrated"] == 2

    def test_outbox_added_to_memory(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        run_bridge_migration(sonder_home)
        conn = sqlite3.connect(str(sonder_home / "memory.db"))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "outbox_events" in tables
        conn.close()

    def test_all_dbs_have_epoch_2(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        run_bridge_migration(sonder_home)
        for name in ["memory.db", "automation.db", "operations.db",
                      "selfmod.db", "training.db"]:
            assert check_epoch(sonder_home / name) == 2, f"{name} missing epoch 2"

    def test_all_dbs_have_outbox(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        run_bridge_migration(sonder_home)
        for name in ["memory.db", "automation.db", "selfmod.db", "training.db"]:
            conn = sqlite3.connect(str(sonder_home / name))
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "outbox_events" in tables, f"{name} missing outbox_events"
            conn.close()

    def test_memory_data_preserved(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        run_bridge_migration(sonder_home)
        conn = sqlite3.connect(str(sonder_home / "memory.db"))
        count = conn.execute("SELECT count(*) FROM interactions").fetchone()[0]
        assert count == 1
        conn.close()

    def test_idempotent_migration(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        run_bridge_migration(sonder_home)
        # Running again should not fail or duplicate
        receipt2 = run_bridge_migration(sonder_home, version="spec5-bridge-retry")
        assert receipt2.epoch == EPOCH


class TestCrashRecovery:
    def test_restore_from_backup(self, sonder_home):
        _create_legacy_memory_db(sonder_home)
        receipt = run_bridge_migration(sonder_home)

        # Simulate corruption
        (sonder_home / "memory.db").unlink()

        # Restore from backup
        backup_dir = Path(receipt.backup_path)
        import shutil
        shutil.copy2(str(backup_dir / "memory.db"),
                      str(sonder_home / "memory.db"))

        # Verify restored data
        conn = sqlite3.connect(str(sonder_home / "memory.db"))
        count = conn.execute("SELECT count(*) FROM interactions").fetchone()[0]
        assert count == 1
        conn.close()
