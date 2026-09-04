"""Adapter: SQLite-backed checkpoint storage.

Moved from domain.automation.checkpoint to satisfy the architecture rule
that sqlite3.connect calls live only in the adapters layer.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

from sonder_runtime.domain.automation.checkpoint import Checkpoint

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHECKPOINTS = 20


class CheckpointStore:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        session_id    TEXT NOT NULL,
        step_index    INTEGER NOT NULL,
        status        TEXT NOT NULL,
        context_json  TEXT NOT NULL DEFAULT '{}',
        created_at    REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_cp_session
        ON checkpoints (session_id, step_index);
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        max_checkpoints: int = _DEFAULT_MAX_CHECKPOINTS,
    ) -> None:
        self._db_path = str(db_path)
        self._max_checkpoints = max(1, max_checkpoints)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            path = self._db_path
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(self._SCHEMA)
            self._conn = conn
        return self._conn

    def save(self, checkpoint: Checkpoint) -> str:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO checkpoints "
                "(checkpoint_id, session_id, step_index, status, "
                "context_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    checkpoint.checkpoint_id,
                    checkpoint.session_id,
                    checkpoint.step_index,
                    checkpoint.status,
                    json.dumps(checkpoint.context, default=str),
                    checkpoint.created_at,
                ),
            )
            self._prune(conn, checkpoint.session_id)
            conn.commit()
        return checkpoint.checkpoint_id

    def latest(self, session_id: str) -> Checkpoint | None:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE session_id=? "
                "ORDER BY step_index DESC, created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_checkpoint(row)

    def get(self, checkpoint_id: str) -> Checkpoint | None:
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_checkpoint(row)

    def list_checkpoints(
        self, session_id: str, limit: int = 10,
    ) -> list[Checkpoint]:
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM checkpoints WHERE session_id=? "
                "ORDER BY step_index DESC, created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row_to_checkpoint(r) for r in rows]

    def delete(self, checkpoint_id: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "DELETE FROM checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            )
            conn.commit()
        return cursor.rowcount > 0

    def clear_session(self, session_id: str) -> int:
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "DELETE FROM checkpoints WHERE session_id=?",
                (session_id,),
            )
            conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _prune(self, conn: sqlite3.Connection, session_id: str) -> None:
        conn.execute(
            "DELETE FROM checkpoints WHERE session_id=? "
            "AND checkpoint_id NOT IN ("
            "  SELECT checkpoint_id FROM checkpoints "
            "  WHERE session_id=? "
            "  ORDER BY step_index DESC, created_at DESC "
            "  LIMIT ?"
            ")",
            (session_id, session_id, self._max_checkpoints),
        )

    @staticmethod
    def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
        return Checkpoint(
            checkpoint_id=row["checkpoint_id"],
            session_id=row["session_id"],
            step_index=row["step_index"],
            status=row["status"],
            context=json.loads(row["context_json"]),
            created_at=row["created_at"],
        )


def can_resume(session_id: str, store: CheckpointStore) -> bool:
    cp = store.latest(session_id)
    return cp is not None and cp.status not in ("completed", "failed", "cancelled")


def resume_point(session_id: str, store: CheckpointStore) -> int:
    cp = store.latest(session_id)
    if cp is None:
        return 0
    return cp.step_index


__all__ = [
    "CheckpointStore",
    "can_resume",
    "resume_point",
]
