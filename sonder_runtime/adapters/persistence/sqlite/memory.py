"""Memory domain persistence adapter (SPEC-5 §10).

memory.db owns interactions, outcomes, lessons, sessions, facts,
preferences, and the transactional outbox.  It does NOT own tasks
(moved to automation.db in epoch 2).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .outbox import OUTBOX_DDL


def add_outbox_to_memory_db(conn: sqlite3.Connection) -> None:
    """Add outbox_events table to an existing memory.db.

    Called during the bridge migration to upgrade an epoch-1 memory.db
    to epoch-2 (outbox-enabled) without recreating the database.
    """
    conn.executescript(OUTBOX_DDL)


def add_epoch_marker(conn: sqlite3.Connection) -> None:
    """Create the schema_epoch table if missing."""
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS schema_epoch (
            epoch           INTEGER NOT NULL,
            completed_at    TEXT NOT NULL,
            source_version  TEXT NOT NULL
        )
    """)
