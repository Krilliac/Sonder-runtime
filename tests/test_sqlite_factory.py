"""Tests for sonder_runtime.adapters.persistence.sqlite_factory."""
from __future__ import annotations

import sqlite3

import pytest
import tempfile
import unittest
from pathlib import Path

from sonder_runtime.adapters.persistence.sqlite_factory import (
    cached_connection,
    close_cached,
    connect,
)


class TestConnect(unittest.TestCase):
    def test_memory_db(self):
        conn = connect(":memory:")
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        row = conn.execute("SELECT id FROM t").fetchone()
        self.assertEqual(row["id"], 1)
        conn.close()

    def test_wal_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = connect(db, wal=True)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")
            conn.close()

    def test_no_wal_mode(self):
        conn = connect(":memory:", wal=False)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "memory")
        conn.close()

    def test_foreign_keys(self):
        conn = connect(":memory:", foreign_keys=True)
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(fk, 1)
        conn.close()

    def test_row_factory(self):
        conn = connect(":memory:", row_factory=True)
        self.assertEqual(conn.row_factory, sqlite3.Row)
        conn.close()

    def test_no_row_factory(self):
        conn = connect(":memory:", row_factory=False)
        self.assertIsNone(conn.row_factory)
        conn.close()

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "sub" / "dir" / "test.db"
            conn = connect(db)
            conn.execute("CREATE TABLE t (id INTEGER)")
            conn.close()
            self.assertTrue(db.exists())

    def test_busy_timeout(self):
        conn = connect(":memory:", busy_timeout_ms=10000)
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(timeout, 10000)
        conn.close()


class TestCachedConnection(unittest.TestCase):
    def setUp(self):
        close_cached("test_cache")

    def tearDown(self):
        close_cached("test_cache")

    def test_returns_same_connection(self):
        conn1 = cached_connection("test_cache", ":memory:")
        conn2 = cached_connection("test_cache", ":memory:")
        self.assertIs(conn1, conn2)

    def test_schema_applied(self):
        conn = cached_connection(
            "test_cache", ":memory:",
            schema_sql="CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY);",
        )
        conn.execute("INSERT INTO items VALUES ('a')")
        row = conn.execute("SELECT id FROM items").fetchone()
        self.assertEqual(row["id"], "a")

    def test_different_path_reconnects(self):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                db1 = Path(tmp) / "a.db"
                db2 = Path(tmp) / "b.db"
                conn1 = cached_connection("test_cache", db1)
                conn1.execute("CREATE TABLE t (v TEXT)")
                conn2 = cached_connection("test_cache", db2)
                self.assertIsNot(conn1, conn2)
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn1.execute("SELECT 1")
            finally:
                # tearDown runs after the temporary directory exits.
                close_cached("test_cache")

    def test_close_cached(self):
        cached_connection("test_cache", ":memory:")
        close_cached("test_cache")
        conn = cached_connection("test_cache", ":memory:")
        self.assertIsNotNone(conn)


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("stage", ["row_factory", "busy_timeout", "wal", "foreign_keys"])
def test_failed_factory_setup_closes_the_created_connection(monkeypatch, stage):
    from sonder_runtime.adapters.persistence import sqlite_factory

    failure = ValueError("setup refused") if stage == "row_factory" else sqlite3.OperationalError("setup refused")

    class Connection:
        close_calls = 0

        def __setattr__(self, name, value):
            if name == "row_factory" and stage == name:
                raise failure
            object.__setattr__(self, name, value)

        def execute(self, statement):
            selected = {"busy_timeout": "PRAGMA busy_timeout=", "wal": "PRAGMA journal_mode=WAL",
                        "foreign_keys": "PRAGMA foreign_keys=ON"}.get(stage)
            if selected is not None and statement.startswith(selected):
                raise failure

        def close(self):
            self.close_calls += 1

    connection = Connection()
    monkeypatch.setattr(sqlite_factory, "owned_sqlite_connect", lambda *args, **kwargs: connection)
    with pytest.raises(type(failure)) as err:
        connect(":memory:", foreign_keys=True)
    assert err.value is failure
    assert connection.close_calls == 1


def test_failed_factory_setup_cleanup_has_no_retryable_contention_code(monkeypatch):
    from sonder_runtime.adapters.persistence import sqlite_factory

    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY

    class Connection:
        close_calls = 0

        def execute(self, statement):
            raise busy

        def close(self):
            self.close_calls += 1
            raise busy

    connection = Connection()
    monkeypatch.setattr(sqlite_factory, "owned_sqlite_connect", lambda *args, **kwargs: connection)
    with pytest.raises(sqlite_factory.SQLiteConnectionSetupCleanupError) as err:
        connect(":memory:")
    assert connection.close_calls == 1
    assert err.value.retryable is False
    assert getattr(err.value, "sqlite_errorcode", None) is None
    assert str(err.value) == "SQLite connection setup cleanup failed"



def test_failed_factory_setup_releases_managed_owner_capacity(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence import sqlite_factory
    from sonder_runtime.adapters.persistence.owned_sqlite import OwnedSQLiteConnections

    owner = OwnedSQLiteConnections((tmp_path,), max_connections=1)

    def connect_owned(database, **kwargs):
        connection = owner.connect(database, **kwargs)
        connection.set_authorizer(
            lambda action, name, value, database, trigger:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA and name == "journal_mode" else sqlite3.SQLITE_OK)
        return connection

    monkeypatch.setattr(sqlite_factory, "owned_sqlite_connect", connect_owned)
    with pytest.raises(sqlite3.DatabaseError):
        connect(tmp_path / "setup.sqlite3")
    assert owner.snapshot().clean
    admitted = owner.connect(tmp_path / "setup.sqlite3")
    admitted.close()
    assert owner.snapshot().clean
