"""Automation domain persistence adapter (SPEC-5 §11).

Owns automation.db: tasks, autopilot, fleet, and workflow runs
under a unified automation_runs/automation_steps schema with a kind
discriminator.  Includes outbox_events for transactional event dispatch.
"""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

import json
import logging
import sqlite3
from pathlib import Path

from .outbox import OUTBOX_DDL

logger = logging.getLogger(__name__)


AUTOMATION_DDL = """\
CREATE TABLE IF NOT EXISTS automation_runs (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 0,
    objective       TEXT NOT NULL,
    correlation_id  TEXT NOT NULL,
    config_json     TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_steps (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    status          TEXT NOT NULL,
    command_json    TEXT NOT NULL,
    result_json     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS task_events (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    sequence        INTEGER NOT NULL,
    event_type      TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS automation_claims (
    run_id          TEXT PRIMARY KEY,
    worker_id       TEXT NOT NULL,
    lease_until     TEXT NOT NULL,
    revision        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id              TEXT PRIMARY KEY,
    run_id          TEXT,
    parent_id       TEXT,
    status          TEXT NOT NULL,
    objective       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_epoch (
    epoch           INTEGER NOT NULL,
    completed_at    TEXT NOT NULL,
    source_version  TEXT NOT NULL
);
"""


def init_automation_db(db_path: Path) -> sqlite3.Connection:
    """Open or create automation.db with the SPEC-5 schema."""
    logger.debug(f"initializing automation.db at {db_path!r}")
    conn = owned_sqlite_connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(AUTOMATION_DDL)
    conn.executescript(OUTBOX_DDL)
    logger.debug("automation.db schema applied successfully")
    logger.info(f"automation.db initialized at {db_path}")
    return conn
