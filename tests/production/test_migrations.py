"""SPEC-2 section 7: ledger, checksums, future-schema and edit refusal."""
from __future__ import annotations

import sqlite3

import pytest

import sonder_migrations
from sonder_migrations import (
    FutureSchemaError,
    MigrationError,
    migrate_store,
    status,
)

pytestmark = pytest.mark.unit


def test_operations_baseline_applies_and_records(tmp_path):
    db = str(tmp_path / "operations.db")
    result = migrate_store("operations", db)
    assert result.applied == ("0001_baseline",)
    assert not result.pending
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT migration_id, application_version, checksum_sha256,"
            " duration_ms FROM schema_migrations"
        ).fetchone()
        assert row[0] == "0001_baseline"
        assert row[1]
        assert len(row[2]) == 64
        assert row[3] >= 0
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert {"operation_event", "backup_run", "maintenance_lock"} <= tables


def test_migrate_is_idempotent(tmp_path):
    db = str(tmp_path / "operations.db")
    migrate_store("operations", db)
    again = migrate_store("operations", db)
    assert again.applied == ("0001_baseline",)
    conn = sqlite3.connect(db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_future_schema_rejected(tmp_path):
    db = str(tmp_path / "operations.db")
    migrate_store("operations", db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO schema_migrations VALUES"
            " ('9999_from_the_future', '2999-01-01T00:00:00Z', '99.0', 'x', 1)"
        )
        conn.commit()
    finally:
        conn.close()
    st = status("operations", db)
    assert st.unknown == ("9999_from_the_future",)
    assert not st.healthy
    with pytest.raises(FutureSchemaError):
        migrate_store("operations", db)


def test_edited_migration_history_rejected(tmp_path):
    db = str(tmp_path / "operations.db")
    migrate_store("operations", db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE schema_migrations SET checksum_sha256 = 'tampered'"
            " WHERE migration_id = '0001_baseline'"
        )
        conn.commit()
    finally:
        conn.close()
    st = status("operations", db)
    assert st.checksum_mismatches == ("0001_baseline",)
    with pytest.raises(MigrationError):
        migrate_store("operations", db)


def test_all_registered_stores_report_status():
    statuses = sonder_migrations.status_all()
    assert set(statuses) == {
        "memory", "autopilot", "fleet", "operations", "updates"
    }
    for store_status in statuses.values():
        assert not store_status.unknown
        assert not store_status.checksum_mismatches


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    db = str(tmp_path / "operations.db")

    class BoomMigration(sonder_migrations.Migration):
        def run(self, conn, record_applied=None):
            conn.execute("BEGIN")
            conn.execute("CREATE TABLE half_done (x INTEGER)")
            conn.execute("ROLLBACK")
            raise RuntimeError("boom")

    real = sonder_migrations.discover_migrations("operations")
    boom = BoomMigration(
        store="operations",
        migration_id="0002_boom",
        path=real[0].path,
        checksum="0" * 64,
    )
    monkeypatch.setattr(
        sonder_migrations,
        "discover_migrations",
        lambda store: real + (boom,) if store == "operations" else (),
    )
    with pytest.raises(RuntimeError, match="boom"):
        migrate_store("operations", db)
    conn = sqlite3.connect(db)
    try:
        applied = [
            r[0]
            for r in conn.execute("SELECT migration_id FROM schema_migrations")
        ]
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert applied == ["0001_baseline"]  # baseline committed, boom did not
    assert "half_done" not in tables


def test_schema_and_ledger_insert_are_one_transaction(tmp_path):
    db = str(tmp_path / "updates.db")
    conn = sonder_migrations.open_connection(db)
    try:
        sonder_migrations._ledger_rows(conn)
        conn.execute(
            "CREATE TRIGGER reject_migration_ledger BEFORE INSERT ON schema_migrations "
            "BEGIN SELECT RAISE(ABORT, 'ledger denied'); END"
        )
    finally:
        conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="ledger denied"):
        migrate_store("updates", db)

    conn = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert "installed_release" not in tables
