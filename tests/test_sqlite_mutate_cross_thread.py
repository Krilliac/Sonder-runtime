"""A preview lease must survive an apply dispatched on a different worker thread.

The MCP server runs synchronous tools on anyio worker threads, and nothing pins
a preview and its later apply to the same thread (idle workers are pruned after
10 s; parallel calls use different workers).  The leased connection is only
ever touched by one thread at a time -- the lease is popped under a lock --
so it must not be bound to the thread that opened it.
"""
import sqlite3
import threading

import pytest

import sqlite_mutate as mutate


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    path = tmp_path / "records.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, active INTEGER)")
    conn.executemany("INSERT INTO records VALUES (?, ?)", [(1, 1), (2, 1)])
    conn.commit()
    conn.close()
    return path


def _in_thread(fn):
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # surfaced to the test thread below
            box["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=30)
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_apply_on_another_thread_uses_the_preview_lease(database):
    sql, params = "UPDATE records SET active = ? WHERE id = ?", [0, 1]
    preview = _in_thread(lambda: mutate.mutate_sqlite(database, sql, params))
    result = _in_thread(lambda: mutate.mutate_sqlite(
        database, sql, params, mode="apply",
        preview_token=preview["preview_token"],
    ))
    assert result["applied"] is True
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT active FROM records WHERE id = 1").fetchone() == (0,)
    finally:
        conn.close()


def test_stale_lease_is_closed_from_any_thread(database):
    sql, params = "UPDATE records SET active = ? WHERE id = ?", [0, 2]
    preview = _in_thread(lambda: mutate.mutate_sqlite(database, sql, params))
    with mutate._preview_lease_lock:
        lease = mutate._preview_leases.pop(preview["preview_token"])
    mutate._close_preview_lease(lease)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        lease["conn"].execute("SELECT 1")
