"""SPEC-2 WP5: adoption baselines for the legacy stores."""
from __future__ import annotations

import sqlite3

import pytest

import sonder_migrations

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("store", ["memory", "autopilot", "fleet"])
def test_fresh_database_gets_baseline_and_ledger(store, tmp_path):
    db = str(tmp_path / f"{store}.db")
    status = sonder_migrations.migrate_store(store, db)
    assert status.applied == ("0001_baseline",)
    assert status.current
    conn = sqlite3.connect(db)
    try:
        ledger = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert ledger == 1
    assert len(tables) > 1  # ledger plus real store tables


def test_existing_database_is_adopted_without_data_loss(tmp_path):
    # Simulate an upgrade origin: a memory.db built by the legacy
    # bootstrap, containing data, gains a ledger without losing anything.
    import memory_store

    db = str(tmp_path / "memory.db")
    conn = memory_store.connect(db, check_same_thread=False)
    memory_store.init_db(conn)
    tables_before = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    conn.close()

    status = sonder_migrations.migrate_store("memory", db)
    assert status.applied == ("0001_baseline",)

    conn = sqlite3.connect(db)
    try:
        tables_after = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert tables_before <= tables_after
    assert "schema_migrations" in tables_after
