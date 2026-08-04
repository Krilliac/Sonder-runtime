"""Adoption baseline for fleet.db (SPEC-2 WP5).

Delegates to the idempotent legacy bootstrap so both fresh and existing
databases end up with the current schema and a ledger entry.
"""

manages_own_transaction = True


def _db_path(conn) -> str:
    for _, name, filename in conn.execute("PRAGMA database_list"):
        if name == "main":
            return filename
    raise RuntimeError("cannot resolve database path for fleet baseline")


def apply(conn) -> None:
    import fleet_store

    fleet_store._ensure_schema(_db_path(conn))


def verify(conn) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if not any(t.startswith("fleet_") for t in tables):
        raise RuntimeError("fleet baseline verify failed: no known tables")
