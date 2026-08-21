"""Durable admission receipts for retried HTTP actions.

The process-local lifecycle coordinator coalesces concurrent retries, but it
cannot answer what happened if the server dies while an action is executing.
This tiny store deliberately records *only* an opaque, already-hashed replay
key, an opaque owner scope, and state.  It never stores a prompt, tool
arguments, account identity, or the action result.  After a restart an
admitted action is therefore fail-closed as ``completed`` or ``uncertain``
instead of being silently reissued.

Bounding contract -- replay keys are client-chosen, so admission is what keeps
this store from growing without bound:

* ``completed`` receipts expire: a claim made more than
  ``completed_ttl_seconds()`` after an action completed prunes its receipt,
  and the key is re-admitted as a fresh action.  The TTL therefore *is* the
  documented replay-protection window.
* ``started`` and ``uncertain`` receipts are crash evidence.  They are never
  expired or evicted; only an operator can clear them.
* New keys are admitted only while the table holds fewer than
  ``global_limit()`` rows, and fewer than ``owner_limit()`` rows for the
  claiming owner scope.  At capacity a *new* key is deterministically refused
  with :data:`REJECTED` and nothing is written; replays of retained keys are
  never capacity-rejected.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from pathlib import Path

from sonder_runtime.platform import paths as sonder_paths

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

# Returned by claim() when admitting a new replay key would exceed a bound.
# Deliberately not a table state: a rejected claim writes nothing.
REJECTED = "rejected-capacity"


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def completed_ttl_seconds() -> int:
    """Replay-protection window for completed receipts, in seconds."""
    return max(60, min(
        365 * 86400, _env_int("SONDER_SERVED_RECEIPT_TTL_SECONDS", 7 * 86400)
    ))


def global_limit() -> int:
    """Maximum retained receipts across all owner scopes."""
    return max(1, min(1_000_000, _env_int("SONDER_SERVED_RECEIPT_LIMIT", 4096)))


def owner_limit() -> int:
    """Maximum retained receipts for one owner scope."""
    return max(1, min(
        1_000_000, _env_int("SONDER_SERVED_RECEIPT_OWNER_LIMIT", 512)
    ))


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
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(served_action_receipts)"
                    )
                }
                if "owner_scope" not in columns:
                    conn.execute(
                        "ALTER TABLE served_action_receipts "
                        "ADD COLUMN owner_scope TEXT NOT NULL DEFAULT ''"
                    )
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


def claim(replay_key: str, owner_scope: str = "", now: float | None = None) -> str:
    """Atomically admit an action or return its durable terminal state.

    ``started`` means a prior process may have crossed the side-effect boundary
    and never reported back.  It is intentionally not retried automatically.
    Expired ``completed`` receipts are pruned before lookup, so a key claimed
    past the TTL window is a fresh admission.  A new key that would exceed the
    global or per-owner bound returns :data:`REJECTED` without writing.
    """
    if not replay_key:
        raise ValueError("replay key is required")
    now = time.time() if now is None else float(now)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM served_action_receipts "
            "WHERE state = 'completed' AND updated_ts <= ?",
            (now - completed_ttl_seconds(),),
        )
        row = conn.execute(
            "SELECT state FROM served_action_receipts WHERE replay_key = ?",
            (replay_key,),
        ).fetchone()
        if row is not None:
            conn.commit()
            return str(row[0])
        total = conn.execute(
            "SELECT COUNT(*) FROM served_action_receipts"
        ).fetchone()[0]
        if total >= global_limit():
            conn.commit()
            return REJECTED
        if owner_scope:
            owned = conn.execute(
                "SELECT COUNT(*) FROM served_action_receipts WHERE owner_scope = ?",
                (owner_scope,),
            ).fetchone()[0]
            if owned >= owner_limit():
                conn.commit()
                return REJECTED
        conn.execute(
            "INSERT INTO served_action_receipts"
            "(replay_key, state, created_ts, updated_ts, owner_scope) "
            "VALUES (?, 'started', ?, ?, ?)",
            (replay_key, now, now, owner_scope or ""),
        )
        conn.commit()
        return "claimed"
    finally:
        conn.close()


def finish(replay_key: str, *, uncertain: bool = False,
           now: float | None = None) -> None:
    """Record the observed terminal state after an admitted action returns."""
    conn = _connect()
    try:
        state = "uncertain" if uncertain else "completed"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE served_action_receipts SET state = ?, updated_ts = ? "
            "WHERE replay_key = ? AND state = 'started'",
            (state, time.time() if now is None else float(now), replay_key),
        )
        conn.commit()
    finally:
        conn.close()


def reset_for_tests() -> None:
    """Drop module-local schema identity cache used by temporary test stores."""
    with _LOCK:
        _INITIALIZED.clear()
