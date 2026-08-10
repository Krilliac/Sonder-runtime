"""Updates domain persistence adapter (SPEC-5 §27).

updates.db is already established by SPEC-4.  This adapter adds
the transactional outbox and epoch marker to the existing schema.
"""
from __future__ import annotations

import sqlite3

from .outbox import OUTBOX_DDL


def add_outbox_to_updates_db(conn: sqlite3.Connection) -> None:
    """Add outbox_events table to an existing updates.db."""
    conn.executescript(OUTBOX_DDL)


def add_epoch_marker(conn: sqlite3.Connection) -> None:
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS schema_epoch (
            epoch           INTEGER NOT NULL,
            completed_at    TEXT NOT NULL,
            source_version  TEXT NOT NULL
        )
    """)
