"""Baseline schema for operations.db (SPEC-2 section 8)."""


def preflight(conn) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='operation_event'"
    ).fetchone()
    if row is not None:
        raise RuntimeError(
            "operation_event already exists without a ledger entry; "
            "refusing to re-run the baseline"
        )


def apply(conn) -> None:
    conn.execute(
        """
        CREATE TABLE operation_event (
            event_id TEXT PRIMARY KEY,
            occurred_at_utc TEXT NOT NULL,
            component TEXT NOT NULL,
            event_code TEXT NOT NULL,
            severity TEXT NOT NULL,
            correlation_id TEXT,
            principal_id TEXT,
            operation_id TEXT,
            summary TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            redaction_version INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX operation_event_time_idx"
        " ON operation_event(occurred_at_utc)"
    )
    conn.execute(
        "CREATE INDEX operation_event_operation_idx"
        " ON operation_event(operation_id, occurred_at_utc)"
    )
    conn.execute(
        """
        CREATE TABLE backup_run (
            backup_id TEXT PRIMARY KEY,
            started_at_utc TEXT NOT NULL,
            completed_at_utc TEXT,
            status TEXT NOT NULL CHECK(status IN (
              'planned','running','verified','failed','pruned'
            )),
            application_version TEXT NOT NULL,
            manifest_path TEXT,
            total_bytes INTEGER NOT NULL DEFAULT 0,
            error_code TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE maintenance_lock (
            lock_name TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            acquired_at_utc TEXT NOT NULL,
            expires_at_utc TEXT,
            reason TEXT NOT NULL
        )
        """
    )


def verify(conn) -> None:
    for table in ("operation_event", "backup_run", "maintenance_lock"):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"baseline verify failed: {table} missing")
