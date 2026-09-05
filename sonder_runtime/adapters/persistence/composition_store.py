"""Cross-subsystem binding persistence.

Tracks relationships between Goal, Autopilot, Workflow, Task, Fleet, Training,
SelfMod, and Memory subsystems so that lifecycle events on one can propagate
to the other.

A binding is a directed link: source drives target.  When the source reaches a
terminal state, the composition service consults the binding to decide what
happens to the target (and vice-versa for reverse lookups).

Storage lives beside the goal store in the same per-user state directory.
Every read path fail-softs to empty so a corrupt store never breaks a session.
"""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

import json
import sqlite3
import threading
import time
import uuid

from sonder_runtime.platform import paths as sonder_paths

_LOCAL = threading.local()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_bind_source
    ON bindings(source_type, source_id, status);
CREATE INDEX IF NOT EXISTS idx_bind_target
    ON bindings(target_type, target_id, status);
"""

VALID_TYPES = (
    "goal", "autopilot", "workflow", "task", "fleet",
    "training", "selfmod", "memory", "campaign", "persona",
)
VALID_KINDS = (
    "drives",       # source execution drives target objective
    "tracks",       # source passively tracks target state
    "decomposes",   # source decomposed into target items
    "produces",     # source produces target artifacts/outcomes
    "informs",      # source provides context to target
)
VALID_STATUSES = ("active", "completed", "broken", "superseded")


class CompositionStoreError(RuntimeError):
    pass


def database_path() -> str:
    return sonder_paths.state_path("composition.db", "SONDER_COMPOSITION_DB")


class _suppress_sqlite:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, sqlite3.Error)


def _connection():
    path = database_path()
    conn = getattr(_LOCAL, "comp_conn", None)
    if conn is not None and getattr(_LOCAL, "comp_path", None) == path:
        return conn
    if conn is not None:
        with _suppress_sqlite():
            conn.close()
    conn = owned_sqlite_connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _LOCAL.comp_conn = conn
    _LOCAL.comp_path = path
    return conn


def _row_dict(row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    try:
        d["metadata"] = json.loads(d.pop("metadata_json", "{}"))
    except (TypeError, ValueError):
        d["metadata"] = {}
    return d


def bind(
    source_type: str,
    source_id: str,
    target_type: str,
    target_id: str,
    kind: str = "drives",
    metadata: dict | None = None,
) -> dict:
    if source_type not in VALID_TYPES:
        raise CompositionStoreError("invalid source type: %s" % source_type)
    if target_type not in VALID_TYPES:
        raise CompositionStoreError("invalid target type: %s" % target_type)
    if kind not in VALID_KINDS:
        raise CompositionStoreError("invalid binding kind: %s" % kind)
    binding_id = "bind-%s" % uuid.uuid4().hex[:12]
    now = time.time()
    conn = _connection()
    with conn:
        existing = conn.execute(
            "SELECT id FROM bindings WHERE source_type=? AND source_id=? "
            "AND target_type=? AND target_id=? AND status='active'",
            (source_type, source_id, target_type, target_id),
        ).fetchone()
        if existing:
            return _row_dict(conn.execute(
                "SELECT * FROM bindings WHERE id=?", (existing["id"],)
            ).fetchone())
        conn.execute(
            "INSERT INTO bindings(id, source_type, source_id, target_type, "
            "target_id, kind, status, created, updated, metadata_json) "
            "VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (binding_id, source_type, source_id, target_type, target_id,
             kind, now, now, json.dumps(metadata or {})),
        )
    row = conn.execute(
        "SELECT * FROM bindings WHERE id=?", (binding_id,)
    ).fetchone()
    return _row_dict(row)


def lookup_targets(source_type: str, source_id: str, target_type: str = "") -> list[dict]:
    try:
        conn = _connection()
        if target_type:
            rows = conn.execute(
                "SELECT * FROM bindings WHERE source_type=? AND source_id=? "
                "AND target_type=? AND status='active' ORDER BY created",
                (source_type, source_id, target_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bindings WHERE source_type=? AND source_id=? "
                "AND status='active' ORDER BY created",
                (source_type, source_id),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [_row_dict(r) for r in rows]


def lookup_sources(target_type: str, target_id: str, source_type: str = "") -> list[dict]:
    try:
        conn = _connection()
        if source_type:
            rows = conn.execute(
                "SELECT * FROM bindings WHERE target_type=? AND target_id=? "
                "AND source_type=? AND status='active' ORDER BY created",
                (target_type, target_id, source_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bindings WHERE target_type=? AND target_id=? "
                "AND status='active' ORDER BY created",
                (target_type, target_id),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [_row_dict(r) for r in rows]


def complete_binding(binding_id: str, reason: str = "") -> dict:
    return _close(binding_id, "completed", reason)


def break_binding(binding_id: str, reason: str = "") -> dict:
    return _close(binding_id, "broken", reason)


def _close(binding_id: str, status: str, reason: str) -> dict:
    now = time.time()
    conn = _connection()
    with conn:
        conn.execute(
            "UPDATE bindings SET status=?, updated=?, metadata_json="
            "json_set(metadata_json, '$.close_reason', ?) WHERE id=?",
            (status, now, reason[:500], binding_id),
        )
    row = conn.execute(
        "SELECT * FROM bindings WHERE id=?", (binding_id,)
    ).fetchone()
    return _row_dict(row)


def active_bindings(limit: int = 50) -> list[dict]:
    try:
        conn = _connection()
        rows = conn.execute(
            "SELECT * FROM bindings WHERE status='active' "
            "ORDER BY created DESC LIMIT ?",
            (min(limit, 200),),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [_row_dict(r) for r in rows]


def close_all_for(entity_type: str, entity_id: str, status: str = "completed") -> int:
    if status not in VALID_STATUSES:
        return 0
    now = time.time()
    try:
        conn = _connection()
        with conn:
            cursor = conn.execute(
                "UPDATE bindings SET status=?, updated=? "
                "WHERE status='active' AND ("
                "(source_type=? AND source_id=?) OR "
                "(target_type=? AND target_id=?))",
                (status, now, entity_type, entity_id, entity_type, entity_id),
            )
        return cursor.rowcount
    except sqlite3.Error:
        return 0


__all__ = [
    "CompositionStoreError",
    "active_bindings",
    "bind",
    "break_binding",
    "close_all_for",
    "complete_binding",
    "database_path",
    "lookup_sources",
    "lookup_targets",
]
