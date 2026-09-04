"""Memory domain persistence adapter (SPEC-5 §10).

memory.db owns interactions, outcomes, lessons, sessions, facts,
preferences, and the transactional outbox.  It does NOT own tasks
(moved to automation.db in epoch 2).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .outbox import OUTBOX_DDL

logger = logging.getLogger(__name__)


def add_outbox_to_memory_db(conn: sqlite3.Connection) -> None:
    """Add outbox_events table to an existing memory.db.

    Called during the bridge migration to upgrade an epoch-1 memory.db
    to epoch-2 (outbox-enabled) without recreating the database.
    """
    logger.debug("adding outbox table to memory.db (epoch-1 -> epoch-2 migration)")
    conn.executescript(OUTBOX_DDL)
    logger.info("memory.db migrated: outbox table added (epoch-1 -> epoch-2)")


def add_epoch_marker(conn: sqlite3.Connection) -> None:
    """Create the schema_epoch table if missing."""
    logger.debug("ensuring schema_epoch table exists in memory.db")
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS schema_epoch (
            epoch           INTEGER NOT NULL,
            completed_at    TEXT NOT NULL,
            source_version  TEXT NOT NULL
        )
    """)
    logger.info("memory.db epoch marker table ensured")
