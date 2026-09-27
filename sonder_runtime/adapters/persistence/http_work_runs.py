"""Durable records of routed HTTP work runs: status, cancel flag, and answer.

A routed workbench/fleet/autopilot turn can outlive the HTTP request that
started it (client timeout, proxy timeout, or the served wait budget).  This
store is what lets the caller come back for the answer, ask for the run to
stop, and learn after a restart that a run was interrupted rather than
silently lost.

It retains only an opaque run id, an opaque owner scope (a digest of the
principal, never an account name), timestamps, a closed status, the cancel
flag, and the bounded answer text the caller would otherwise have received in
the chat reply.  The database is created ``0600``.

Bounds: answers are truncated to ``MAX_OUTPUT_CHARS``; finished rows expire
after ``retention_seconds()`` and the table keeps at most ``row_limit()`` rows
(oldest finished first).  ``running`` rows are never pruned; a row left
``running`` by an earlier process is reconciled to ``interrupted``.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from pathlib import Path

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect
from sonder_runtime.platform import paths as sonder_paths

STATUSES = (
    "running", "returned", "unknown", "refused", "cancelled",
    "budget_exceeded", "interrupted", "failed",
)
TERMINAL_STATUSES = frozenset(STATUSES) - {"running"}
MAX_OUTPUT_CHARS = 256 * 1024

_LOCK = threading.RLock()
_INITIALIZED: set[str] = set()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_work_runs (
    run_id TEXT PRIMARY KEY,
    owner_scope TEXT NOT NULL,
    process_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (%s)),
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    deadline_ts REAL NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    output TEXT NOT NULL DEFAULT '',
    output_truncated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS http_work_runs_owner ON http_work_runs(owner_scope, created_ts);
""" % ", ".join("'%s'" % status for status in STATUSES)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def retention_seconds() -> int:
    return max(3600, min(30 * 86400, _env_int("SONDER_HTTP_WORK_RUN_RETENTION_SECONDS", 7 * 86400)))


def row_limit() -> int:
    return max(16, min(100_000, _env_int("SONDER_HTTP_WORK_RUN_LIMIT", 512)))


def database_path() -> str:
    return sonder_paths.state_path("http_work_runs.db", "SONDER_HTTP_WORK_RUNS_DB")


def _connect() -> sqlite3.Connection:
    path = str(Path(database_path()).expanduser().resolve())
    with _LOCK:
        if path not in _INITIALIZED:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = owned_sqlite_connect(path, timeout=5)
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
    conn = owned_sqlite_connect(path, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _public(row) -> dict:
    return {
        "id": row["run_id"],
        "status": row["status"],
        "created_at": row["created_ts"],
        "updated_at": row["updated_ts"],
        "deadline_at": row["deadline_ts"],
        "cancel_requested": bool(row["cancel_requested"]),
        "output": row["output"],
        "output_truncated": bool(row["output_truncated"]),
    }


def _prune(conn: sqlite3.Connection, now: float) -> None:
    conn.execute(
        "DELETE FROM http_work_runs WHERE status != 'running' AND updated_ts <= ?",
        (now - retention_seconds(),),
    )
    excess = conn.execute("SELECT COUNT(*) FROM http_work_runs").fetchone()[0] - row_limit()
    if excess > 0:
        conn.execute(
            "DELETE FROM http_work_runs WHERE run_id IN ("
            "SELECT run_id FROM http_work_runs WHERE status != 'running' "
            "ORDER BY updated_ts ASC LIMIT ?)",
            (excess,),
        )


def start(run_id: str, *, owner_scope: str, process_id: str, deadline_ts: float,
          now: float | None = None) -> None:
    """Record a newly admitted run as ``running``."""
    if not run_id or not owner_scope or not process_id:
        raise ValueError("run id, owner scope, and process id are required")
    now = time.time() if now is None else float(now)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _prune(conn, now)
        conn.execute(
            "INSERT INTO http_work_runs(run_id, owner_scope, process_id, status, "
            "created_ts, updated_ts, deadline_ts) VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (run_id, owner_scope, process_id, now, now, float(deadline_ts)),
        )
        conn.commit()
    finally:
        conn.close()


def finish(run_id: str, status: str, output: str = "", *, now: float | None = None) -> None:
    """Record the terminal status and bounded answer of a running run."""
    if status not in TERMINAL_STATUSES:
        raise ValueError("terminal work-run status required")
    text = output if isinstance(output, str) else ""
    truncated = len(text) > MAX_OUTPUT_CHARS
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE http_work_runs SET status = ?, output = ?, output_truncated = ?, "
            "updated_ts = ? WHERE run_id = ? AND status = 'running'",
            (status, text[:MAX_OUTPUT_CHARS], int(truncated),
             time.time() if now is None else float(now), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def request_cancel(run_id: str, *, owner_scope: str) -> dict | None:
    """Set the cancel flag on the caller's run; ``None`` when not theirs."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE http_work_runs SET cancel_requested = 1, updated_ts = ? "
            "WHERE run_id = ? AND owner_scope = ? AND status = 'running'",
            (time.time(), run_id, owner_scope),
        )
        row = conn.execute(
            "SELECT * FROM http_work_runs WHERE run_id = ? AND owner_scope = ?",
            (run_id, owner_scope),
        ).fetchone()
        conn.commit()
        return _public(row) if row is not None else None
    finally:
        conn.close()


def cancel_requested(run_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT cancel_requested FROM http_work_runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        # A run whose record vanished cannot prove it may still act.
        return True if row is None else bool(row[0])
    finally:
        conn.close()


def get(run_id: str, *, owner_scope: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM http_work_runs WHERE run_id = ? AND owner_scope = ?",
            (run_id, owner_scope),
        ).fetchone()
        return _public(row) if row is not None else None
    finally:
        conn.close()


def recent(*, owner_scope: str, limit: int = 20) -> list[dict]:
    """The caller's newest runs, without answer text (fetch one run for it)."""
    limit = max(1, min(100, int(limit)))
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM http_work_runs WHERE owner_scope = ? "
            "ORDER BY created_ts DESC LIMIT ?",
            (owner_scope, limit),
        ).fetchall()
    finally:
        conn.close()
    summaries = []
    for row in rows:
        item = _public(row)
        item.pop("output")
        summaries.append(item)
    return summaries


def reconcile(process_id: str, *, now: float | None = None) -> int:
    """Mark runs left ``running`` by any other process as ``interrupted``."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE http_work_runs SET status = 'interrupted', updated_ts = ? "
            "WHERE status = 'running' AND process_id != ?",
            (time.time() if now is None else float(now), process_id),
        ).rowcount
        conn.commit()
        return int(changed or 0)
    finally:
        conn.close()


def reset_for_tests() -> None:
    with _LOCK:
        _INITIALIZED.clear()


__all__ = [
    "MAX_OUTPUT_CHARS", "STATUSES", "TERMINAL_STATUSES", "cancel_requested",
    "database_path", "finish", "get", "recent", "reconcile", "request_cancel",
    "reset_for_tests", "retention_seconds", "row_limit", "start",
]
