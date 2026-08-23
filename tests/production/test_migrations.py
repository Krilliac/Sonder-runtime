"""SPEC-2 section 7: ledger, checksums, future-schema and edit refusal."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

import sonder_runtime.adapters.persistence.migrations as sonder_migrations
from sonder_runtime.adapters.persistence.migrations import (
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
        conn.execute("DROP TRIGGER schema_migrations_no_update")
        conn.execute("DROP TRIGGER schema_migrations_no_delete")
        conn.commit()
    finally:
        conn.close()
    st = status("operations", db)
    assert st.unknown == ("9999_from_the_future",)
    assert not st.healthy
    with pytest.raises(FutureSchemaError):
        migrate_store("operations", db)
    conn = sqlite3.connect(db)
    try:
        guards = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND name LIKE 'schema_migrations_no_%'"
        ).fetchall()
    finally:
        conn.close()
    assert guards == [], "an older build must not modify a future database"


def test_migration_ledger_is_append_only(tmp_path):
    db = str(tmp_path / "operations.db")
    migrate_store("operations", db)
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "UPDATE schema_migrations SET checksum_sha256 = 'tampered'"
                " WHERE migration_id = '0001_baseline'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(
                "DELETE FROM schema_migrations"
                " WHERE migration_id = '0001_baseline'"
            )
    finally:
        conn.close()


def test_edited_migration_source_is_reported_as_checksum_mismatch(
    tmp_path, monkeypatch
):
    db = str(tmp_path / "operations.db")
    migrate_store("operations", db)
    real = sonder_migrations.discover_migrations("operations")
    edited = sonder_migrations.Migration(
        store=real[0].store,
        migration_id=real[0].migration_id,
        path=real[0].path,
        checksum="0" * 64,
    )
    monkeypatch.setattr(
        sonder_migrations,
        "discover_migrations",
        lambda store: (edited,) if store == "operations" else (),
    )

    st = status("operations", db)
    assert st.checksum_mismatches == ("0001_baseline",)
    with pytest.raises(MigrationError):
        migrate_store("operations", db)


def test_migration_source_replacement_after_discovery_fails_closed(tmp_path):
    source = tmp_path / "0001_race.py"
    source.write_text(
        "def apply(conn):\n    conn.execute('CREATE TABLE expected (x INTEGER)')\n",
        encoding="utf-8",
    )
    migration = sonder_migrations.Migration(
        store="race",
        migration_id="0001_race",
        path=source,
        checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    source.write_text(
        "def apply(conn):\n    conn.execute('CREATE TABLE replaced (x INTEGER)')\n",
        encoding="utf-8",
    )
    conn = sonder_migrations.open_connection(str(tmp_path / "race.db"))
    try:
        with pytest.raises(MigrationError, match="changed after discovery"):
            migration.run(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert "expected" not in tables
    assert "replaced" not in tables


def test_migration_cannot_commit_framework_transaction(tmp_path):
    source = tmp_path / "0001_commit.py"
    source.write_text(
        "def apply(conn):\n"
        "    conn.execute('CREATE TABLE escaped (x INTEGER)')\n"
        "    conn.commit()\n",
        encoding="utf-8",
    )
    migration = sonder_migrations.Migration(
        store="commit",
        migration_id="0001_commit",
        path=source,
        checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    conn = sonder_migrations.open_connection(str(tmp_path / "commit.db"))
    try:
        with pytest.raises(MigrationError, match="attempted to control"):
            migration.run(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert "escaped" not in tables


def test_read_only_status_does_not_create_missing_database(tmp_path):
    db = tmp_path / "operations.db"

    result = sonder_migrations.status_read_only("operations", str(db))

    assert result.applied == ()
    assert result.pending == ("0001_baseline",)
    assert not db.exists()


def test_read_only_status_does_not_create_missing_ledger(tmp_path):
    db = tmp_path / "operations.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE existing_state (value TEXT)")
        conn.commit()
    finally:
        conn.close()

    result = sonder_migrations.status_read_only("operations", str(db))

    assert result.pending == ("0001_baseline",)
    conn = sqlite3.connect(db)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert names == {"existing_state"}


def test_immutable_backup_status_creates_no_wal_sidecars(tmp_path):
    db = tmp_path / "operations.db"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE existing_state (value TEXT)")
        conn.commit()
    finally:
        conn.close()

    result = sonder_migrations.status_read_only(
        "operations", str(db), immutable=True
    )

    assert result.pending == ("0001_baseline",)
    assert not (tmp_path / "operations.db-wal").exists()
    assert not (tmp_path / "operations.db-shm").exists()


def test_all_registered_stores_report_status():
    statuses = sonder_migrations.status_all()
    assert set(statuses) == {
        "memory", "autopilot", "fleet", "operations", "queued_actions",
        "updates", "jobs",
    }
    for store_status in statuses.values():
        assert not store_status.unknown
        assert not store_status.checksum_mismatches


def test_store_names_matches_the_path_registry():
    """STORE_NAMES is the literal callers (the CLI, the update bundler/guard)
    use instead of calling store_db_paths(), which touches disk. A name added
    to one and not the other silently drops a store from those surfaces."""
    assert set(sonder_migrations.STORE_NAMES) == set(
        sonder_migrations.store_db_paths()
    )
    assert len(sonder_migrations.STORE_NAMES) == len(
        set(sonder_migrations.STORE_NAMES)
    )


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


def test_lost_migrations_directory_is_refused_not_reported_healthy(
    tmp_path, monkeypatch
):
    """A release shipped without its `migrations/` tree must not journal `ok`.

    `migrate_store` returned `status(...)` the moment `discover_migrations`
    found nothing -- *before* the unknown/checksum gates. So a build whose
    `migrations/` directory was omitted from the bundle ran against a database
    full of applied history, discovered nothing to compare it against, and
    reported a clean status with empty `applied` and empty `pending`. The one
    signal that the build had lost its schema definitions -- every ledger row
    now being `unknown` -- was computed by `status()` and then discarded.
    """
    db = str(tmp_path / "operations.db")
    migrate_store("operations", db)  # a real ledger with real history

    empty = tmp_path / "no-migrations"
    empty.mkdir()
    monkeypatch.setattr(sonder_migrations, "MIGRATIONS_ROOT", empty)

    assert sonder_migrations.discover_migrations("operations") == ()
    st = status("operations", db)
    assert st.unknown == ("0001_baseline",), "precondition: the ledger is now unknown"

    with pytest.raises(FutureSchemaError):
        migrate_store("operations", db)


def test_empty_store_with_no_migrations_and_no_ledger_is_still_fine(
    tmp_path, monkeypatch
):
    """The refusal must key on lost history, not merely on an empty build."""
    empty = tmp_path / "no-migrations"
    empty.mkdir()
    monkeypatch.setattr(sonder_migrations, "MIGRATIONS_ROOT", empty)
    result = migrate_store("operations", str(tmp_path / "fresh.db"))
    assert result.applied == ()
    assert result.unknown == ()
