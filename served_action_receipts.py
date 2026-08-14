"""Durable admission receipts for retried HTTP actions.

The process-local lifecycle coordinator coalesces concurrent retries, but it
cannot answer what happened if the server dies while an action is executing.
This tiny store deliberately records *only* an opaque, already-hashed replay
key and state.  It never stores a prompt, tool arguments, account identity, or
the action result.  After a restart an admitted action is therefore fail-closed
as ``completed`` or ``uncertain`` instead of being silently reissued.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from pathlib import Path

import sonder_paths

_LOCK = threading.RLock()
_INITIALIZED: set[str] = set()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS served_action_receipts (
    replay_key TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('started', 'completed', 'uncertain')),
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
"""


def database_path() -> str:
    return sonder_paths.state_path(
        "served_action_receipts.db", "SONDER_SERVED_ACTION_RECEIPTS_DB"
    )


def _connect() -> sqlite3.Connection:
    path = str(Path(database_path()).expanduser().resolve())
    with _LOCK:
        if path not in _INITIALIZED:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=5)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            if os.name != "nt":
                with contextlib.suppress(OSError):
                    os.chmod(path, 0o600)
            _INITIALIZED.add(path)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def claim(replay_key: str) -> str:
    """Atomically admit an action or return its durable terminal state.

    ``started`` means a prior process may have crossed the side-effect boundary
    and never reported back.  It is intentionally not retried automatically.
    """
    if not replay_key:
        raise ValueError("replay key is required")
    now = time.time()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state FROM served_action_receipts WHERE replay_key = ?",
            (replay_key,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO served_action_receipts(replay_key, state, created_ts, updated_ts) "
                "VALUES (?, 'started', ?, ?)",
                (replay_key, now, now),
            )
            conn.commit()
            return "claimed"
        conn.commit()
        return str(row[0])
    finally:
        conn.close()


def finish(replay_key: str, *, uncertain: bool = False) -> None:
    """Record the observed terminal state after an admitted action returns."""
    conn = _connect()
    try:
        state = "uncertain" if uncertain else "completed"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE served_action_receipts SET state = ?, updated_ts = ? "
            "WHERE replay_key = ? AND state = 'started'",
            (state, time.time(), replay_key),
        )
        conn.commit()
    finally:
        conn.close()


def reset_for_tests() -> None:
    """Drop module-local schema identity cache used by temporary test stores."""
    with _LOCK:
        _INITIALIZED.clear()
