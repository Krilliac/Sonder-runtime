"""Self-modification domain persistence adapter (SPEC-5 §19).

selfmod.db owns the self-modification lifecycle: runs, events, files,
tests, snapshots, and phase transitions.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from .outbox import OUTBOX_DDL

logger = logging.getLogger(__name__)


SELFMOD_DDL = """\
CREATE TABLE IF NOT EXISTS selfmod_runs (
    id                TEXT PRIMARY KEY,
    objective         TEXT NOT NULL,
    mode              TEXT NOT NULL,
    phase             TEXT NOT NULL,
    revision          INTEGER NOT NULL DEFAULT 0,
    repository_path   TEXT NOT NULL,
    starting_revision TEXT,
    unrestricted      INTEGER NOT NULL DEFAULT 0,
    correlation_id    TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS selfmod_events (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    sequence     INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS selfmod_files (
    run_id          TEXT NOT NULL,
    path            TEXT NOT NULL,
    before_sha256   TEXT,
    after_sha256    TEXT,
    existed_before  INTEGER NOT NULL,
    change_type     TEXT NOT NULL,
    PRIMARY KEY(run_id, path)
);

CREATE TABLE IF NOT EXISTS selfmod_tests (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    command_json   TEXT NOT NULL,
    exit_code      INTEGER,
    duration_ms    INTEGER,
    output_digest  TEXT,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS selfmod_snapshots (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    manifest_path   TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS selfmod_transitions (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    from_phase     TEXT,
    to_phase       TEXT NOT NULL,
    revision       INTEGER NOT NULL,
    reason         TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_epoch (
    epoch           INTEGER NOT NULL,
    completed_at    TEXT NOT NULL,
    source_version  TEXT NOT NULL
);
"""


def init_selfmod_db(db_path: Path) -> sqlite3.Connection:
    logger.debug(f"initializing selfmod.db at {db_path!r}")
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SELFMOD_DDL)
    conn.executescript(OUTBOX_DDL)
    logger.debug("selfmod.db schema applied successfully")
    logger.info(f"selfmod.db initialized at {db_path}")
    return conn
