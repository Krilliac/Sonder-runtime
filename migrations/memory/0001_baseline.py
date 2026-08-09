"""Adoption baseline for memory.db (SPEC-2 WP5).

memory.db predates the migration framework; its schema is built by
``memory_store.init_db``/``_migrate`` with idempotent DDL.  This baseline
runs that legacy bootstrap (fresh and existing databases alike) and
records the result in the ledger, giving the store a versioned starting
point for future migrations without rewriting its history.
"""

manages_own_transaction = True


def _db_path(conn) -> str:
    for _, name, filename in conn.execute("PRAGMA database_list"):
        if name == "main":
            return filename
    raise RuntimeError("cannot resolve database path for memory baseline")


def apply(conn) -> None:
    import memory_store

    path = _db_path(conn)
    legacy = memory_store.connect(path, check_same_thread=False)
    try:
        memory_store.init_db(legacy)
    finally:
        legacy.close()


def verify(conn) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "interactions" not in tables and "facts" not in tables:
        raise RuntimeError("memory baseline verify failed: no known tables")
