"""Tests for sonder_runtime.adapters.persistence.sqlite_factory."""
from __future__ import annotations

import sqlite3
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
            db1 = Path(tmp) / "a.db"
            db2 = Path(tmp) / "b.db"
            conn1 = cached_connection("test_cache", db1)
            conn1.execute("CREATE TABLE t (v TEXT)")
            conn2 = cached_connection("test_cache", db2)
            self.assertIsNot(conn1, conn2)

    def test_close_cached(self):
        cached_connection("test_cache", ":memory:")
        close_cached("test_cache")
        conn = cached_connection("test_cache", ":memory:")
        self.assertIsNotNone(conn)


if __name__ == "__main__":
    unittest.main()
