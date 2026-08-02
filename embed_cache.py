"""Revision-pinned embedding cache. Stdlib only. Soft-fails to a miss.

Embeddings are deterministic for a fixed (model identity, serving revision,
text), so re-embedding the same text through Ollama or the NPU worker is pure
waste — memory search and grounding re-embed the same queries constantly.
This cache stores vectors keyed by the SHA-256 of exactly that triple. The
revision component means a retagged or updated Ollama model invalidates every
stale entry for free, preserving the vector-space guarantees in NPU.md:
vectors from different weights can never mix.

The cache is an optimization layer with the same fail-soft contract as
``embeddings.embed``: any sqlite error is a miss (reads) or a no-op (writes),
never an exception into the caller. Rows are evicted least-recently-used once
the row cap is exceeded.

Environment knobs: ``SONDER_EMBED_CACHE=0`` disables the cache entirely;
``SONDER_EMBED_CACHE_DB`` overrides the database path;
``SONDER_EMBED_CACHE_MAX`` caps stored rows (default 50000).
"""
from __future__ import annotations

import array
import hashlib
import os
import sqlite3
import threading
import time

import sonder_paths

_LOCAL = threading.local()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS vectors (
    key TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    dimension INTEGER NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    last_used REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS vectors_last_used ON vectors(last_used);
"""


def enabled() -> bool:
    return os.environ.get("SONDER_EMBED_CACHE", "1").strip() != "0"


def _max_rows() -> int:
    try:
        return max(100, int(os.environ.get("SONDER_EMBED_CACHE_MAX", "50000")))
    except ValueError:
        return 50000


def _db_path() -> str:
    return sonder_paths.state_path("embed-cache.db", "SONDER_EMBED_CACHE_DB")


def _connection():
    path = _db_path()
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None and getattr(_LOCAL, "path", None) == path:
        return conn
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    conn = sqlite3.connect(path, timeout=2.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    _LOCAL.conn = conn
    _LOCAL.path = path
    return conn


def cache_key(text, identity, revision) -> str:
    digest = hashlib.sha256()
    for part in (identity, revision, text):
        digest.update(str(part or "").encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()


def get(text, identity, revision):
    """Cached vector for the exact (identity, revision, text), or None."""
    if not enabled() or not text or not identity or not revision:
        return None
    try:
        conn = _connection()
        row = conn.execute(
            "SELECT vector, dimension FROM vectors WHERE key = ?",
            (cache_key(text, identity, revision),),
        ).fetchone()
        if row is None:
            return None
        payload, dimension = row
        vector = array.array("f")
        vector.frombytes(payload)
        if len(vector) != dimension or dimension <= 0:
            return None
        conn.execute(
            "UPDATE vectors SET last_used = ? WHERE key = ?",
            (time.time(), cache_key(text, identity, revision)),
        )
        conn.commit()
        return [float(value) for value in vector]
    except (sqlite3.Error, OSError, ValueError):
        return None


def put(text, identity, revision, vector, provider="") -> bool:
    """Store a vector; best-effort, False on any failure."""
    if not enabled() or not text or not identity or not revision:
        return False
    if not isinstance(vector, (list, tuple)) or not vector:
        return False
    try:
        payload = array.array("f", [float(value) for value in vector])
    except (TypeError, ValueError, OverflowError):
        return False
    now = time.time()
    try:
        conn = _connection()
        conn.execute(
            "INSERT OR REPLACE INTO vectors "
            "(key, vector, dimension, provider, created, last_used) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                cache_key(text, identity, revision), payload.tobytes(),
                len(vector), str(provider or "")[:32], now, now,
            ),
        )
        cap = _max_rows()
        count = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        if count > cap:
            conn.execute(
                "DELETE FROM vectors WHERE key IN ("
                "SELECT key FROM vectors ORDER BY last_used ASC LIMIT ?)",
                (max(1, count - cap + cap // 10),),
            )
        conn.commit()
        return True
    except (sqlite3.Error, OSError, ValueError):
        return False


def stats() -> dict:
    """Bounded cache statistics for diagnostics; zeros on any failure."""
    if not enabled():
        return {"enabled": False, "rows": 0, "bytes": 0}
    try:
        conn = _connection()
        rows = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        size = 0
        try:
            size = os.stat(_db_path()).st_size
        except OSError:
            pass
        return {"enabled": True, "rows": int(rows), "bytes": int(size)}
    except (sqlite3.Error, OSError):
        return {"enabled": True, "rows": 0, "bytes": 0}


def reset_for_tests():
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _LOCAL.conn = None
    _LOCAL.path = None
