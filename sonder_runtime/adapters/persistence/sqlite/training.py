"""Training domain persistence adapter (SPEC-5 §24).

training.db owns the attended adapter-training lifecycle: runs,
artifacts, checkpoints, evaluations, deployments, and transitions.
"""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

import logging
import sqlite3
from pathlib import Path

from .outbox import OUTBOX_DDL

logger = logging.getLogger(__name__)


TRAINING_DDL = """\
CREATE TABLE IF NOT EXISTS training_runs (
    id                  TEXT PRIMARY KEY,
    state               TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    backend             TEXT NOT NULL,
    base_model          TEXT NOT NULL,
    base_revision       TEXT NOT NULL,
    dataset_sha256      TEXT NOT NULL,
    plan_path           TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_artifacts (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    artifact_type  TEXT NOT NULL,
    path           TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_checkpoints (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL,
    step             INTEGER NOT NULL,
    path             TEXT NOT NULL,
    manifest_sha256  TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_evaluations (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL,
    evaluator_version  TEXT NOT NULL,
    result             TEXT NOT NULL,
    metrics_json       TEXT NOT NULL,
    receipt_sha256     TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
    id            TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    model_name    TEXT NOT NULL UNIQUE,
    model_digest  TEXT NOT NULL,
    state         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    activated_at  TEXT
);

CREATE TABLE IF NOT EXISTS deployment_transitions (
    id                  TEXT PRIMARY KEY,
    deployment_id       TEXT NOT NULL,
    previous_model      TEXT,
    next_model          TEXT NOT NULL,
    policy_revision     INTEGER NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_epoch (
    epoch           INTEGER NOT NULL,
    completed_at    TEXT NOT NULL,
    source_version  TEXT NOT NULL
);
"""


def init_training_db(db_path: Path) -> sqlite3.Connection:
    logger.debug(f"initializing training.db at {db_path!r}")
    conn = owned_sqlite_connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(TRAINING_DDL)
    conn.executescript(OUTBOX_DDL)
    logger.debug("training.db schema applied successfully")
    logger.info(f"training.db initialized at {db_path}")
    return conn
