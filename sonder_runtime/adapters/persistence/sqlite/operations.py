"""Operations domain persistence adapter (SPEC-5 §33).

operations.db is a consolidated operational projection — not the
transactional source of truth for other domains.  It receives events
via the outbox dispatcher and stores operations, dependency health,
and recovery records.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .outbox import OPERATIONS_EVENT_DDL

logger = logging.getLogger(__name__)


OPERATIONS_DDL = """\
CREATE TABLE IF NOT EXISTS operations (
    id              TEXT PRIMARY KEY,
    correlation_id  TEXT NOT NULL,
    operation_type  TEXT NOT NULL,
    source          TEXT NOT NULL,
    state           TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    error_code      TEXT
);

CREATE TABLE IF NOT EXISTS dependency_health (
    name            TEXT PRIMARY KEY,
    status          TEXT NOT NULL,
    last_checked    TEXT NOT NULL,
    detail_json     TEXT
);

CREATE TABLE IF NOT EXISTS recovery_records (
    id              TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    operation_id    TEXT,
    action          TEXT NOT NULL,
    detail_json     TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_epoch (
    epoch           INTEGER NOT NULL,
    completed_at    TEXT NOT NULL,
    source_version  TEXT NOT NULL
);
"""


def init_operations_db(db_path: Path) -> sqlite3.Connection:
    """Open or create operations.db with the SPEC-5 schema."""
    logger.debug(f"initializing operations.db at {db_path!r}")
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(OPERATIONS_DDL)
    conn.executescript(OPERATIONS_EVENT_DDL)
    logger.debug("operations.db schema applied successfully")
    logger.info(f"operations.db initialized at {db_path}")
    return conn
