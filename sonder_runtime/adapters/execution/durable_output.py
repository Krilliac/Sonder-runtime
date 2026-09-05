"""Durable execution-output spill adapter (EXEC-004).

The adapter owns spill bytes in a small SQLite store and exposes only the
existing provider-neutral ``SpillReference`` to execution consumers.  A
reference is useful only when the stored bytes still match its digest and
declared size; reads therefore verify both before returning any payload.
"""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any
from uuid import uuid4

from sonder_runtime.application.execution.world_control import SpillReference
from sonder_runtime.application.ports.artifact_store import (
    ArtifactHandle, SpillHandle, SpillSnapshot, SpillSpec, SpillState,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_DDL = """
CREATE TABLE IF NOT EXISTS execution_spill (
    spill_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    max_bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    name TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    digest TEXT,
    payload BLOB,
    expires_at TEXT,
    created_at TEXT NOT NULL
)
"""


class DurableSpillIntegrityError(ValueError):
    """Stored output cannot satisfy its immutable reference."""


class _SQLiteSpillHandle:
    def __init__(self, store: "SQLiteSpillStore", spill_id: str):
        self._store = store
        self._spill_id = spill_id
        self._closed = False

    @property
    def spill_id(self) -> str:
        return self._spill_id

    def snapshot(self) -> SpillSnapshot:
        return self._store._snapshot(self._spill_id)

    def write(self, chunk: bytes) -> int:
        if self._closed:
            raise ValueError("spill handle is closed")
        return self._store._write(self._spill_id, chunk)

    def commit(self) -> ArtifactHandle:
        if self._closed:
            raise ValueError("spill handle is closed")
        return self._store._commit(self._spill_id)

    def abort(self) -> None:
        if not self._closed:
            self._store._abort(self._spill_id)

    def close(self) -> None:
        self._closed = True


class SQLiteSpillStore:
    """Durable, bounded implementation of the typed ``SpillStore`` port."""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with self._connect() as connection:
            connection.execute(_DDL)

    def _connect(self) -> sqlite3.Connection:
        connection = owned_sqlite_connect(str(self._path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def begin(self, spec: SpillSpec) -> SpillHandle:
        if not isinstance(spec, SpillSpec):
            raise TypeError("spec must be a SpillSpec")
        spill_id = f"spill-{uuid4().hex}"
        expires_at = None
        if spec.ttl_seconds is not None:
            expires_at = datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + spec.ttl_seconds,
                timezone.utc,
            ).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO execution_spill(spill_id,state,max_bytes,media_type,name,expires_at,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (spill_id, SpillState.OPEN.value, spec.max_bytes, spec.media_type,
                 spec.name, expires_at, _now()),
            )
        return _SQLiteSpillHandle(self, spill_id)

    def _row(self, spill_id: str) -> tuple[Any, ...]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT spill_id,state,max_bytes,media_type,name,size_bytes,digest,payload "
                "FROM execution_spill WHERE spill_id=?", (spill_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown spill {spill_id!r}")
        return row

    def _snapshot(self, spill_id: str) -> SpillSnapshot:
        row = self._row(spill_id)
        artifact = None
        if row[1] == SpillState.COMMITTED.value:
            artifact = ArtifactHandle(row[0], row[5], row[6], row[3], row[4])
        return SpillSnapshot(row[0], SpillState(row[1]), row[5], row[2], artifact)

    def _write(self, spill_id: str, chunk: bytes) -> int:
        if not isinstance(chunk, bytes):
            raise TypeError("spill chunks must be bytes")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state,max_bytes,size_bytes,payload FROM execution_spill WHERE spill_id=?",
                (spill_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown spill {spill_id!r}")
            if row[0] != SpillState.OPEN.value:
                raise ValueError("spill is not open")
            payload = bytes(row[3] or b"") + chunk
            if len(payload) > row[1]:
                raise ValueError("spill exceeds max_bytes")
            connection.execute(
                "UPDATE execution_spill SET payload=?,size_bytes=? WHERE spill_id=?",
                (sqlite3.Binary(payload), len(payload), spill_id),
            )
            return len(chunk)

    def _commit(self, spill_id: str) -> ArtifactHandle:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state,max_bytes,media_type,name,size_bytes,payload FROM execution_spill WHERE spill_id=?",
                (spill_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown spill {spill_id!r}")
            if row[0] != SpillState.OPEN.value:
                raise ValueError("spill is not open")
            payload = bytes(row[5] or b"")
            digest = hashlib.sha256(payload).hexdigest()
            connection.execute(
                "UPDATE execution_spill SET state=?,digest=? WHERE spill_id=?",
                (SpillState.COMMITTED.value, digest, spill_id),
            )
            return ArtifactHandle(spill_id, len(payload), digest, row[2], row[3])

    def _abort(self, spill_id: str) -> None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT state FROM execution_spill WHERE spill_id=?", (spill_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown spill {spill_id!r}")
            if row[0] == SpillState.OPEN.value:
                connection.execute(
                    "UPDATE execution_spill SET state=?,payload=NULL,size_bytes=0 WHERE spill_id=?",
                    (SpillState.ABORTED.value, spill_id),
                )

    def read(self, handle: ArtifactHandle, *, max_bytes: int) -> bytes:
        if not isinstance(handle, ArtifactHandle):
            raise TypeError("handle must be an ArtifactHandle")
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        if handle.size_bytes > max_bytes:
            raise ValueError("artifact exceeds read bound")
        row = self._row(handle.artifact_id)
        if row[1] != SpillState.COMMITTED.value or row[6] != handle.sha256 or row[5] != handle.size_bytes:
            raise DurableSpillIntegrityError("spill metadata does not match artifact handle")
        payload = bytes(row[7] or b"")
        if len(payload) != handle.size_bytes or hashlib.sha256(payload).hexdigest() != handle.sha256:
            raise DurableSpillIntegrityError("spill payload digest or size mismatch")
        return payload

    def reap(self) -> int:
        now = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM execution_spill WHERE state=? AND expires_at IS NOT NULL AND expires_at<=?",
                (SpillState.OPEN.value, now),
            )
            return cursor.rowcount


@dataclass(frozen=True, slots=True)
class DurableExecutionOutput:
    """Small bridge from execution text output to a durable spill reference."""

    store: SQLiteSpillStore
    max_bytes: int = 1 << 20
    preview_chars: int = 256

    def spill_text(self, text: str, *, owner_id: str, media_type: str = "text/plain") -> SpillReference:
        if not isinstance(text, str) or not text:
            raise ValueError("spill text must be non-empty")
        if not owner_id.strip():
            raise ValueError("owner_id must be non-empty")
        payload = text.encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ValueError("output exceeds spill bound")
        handle = self.store.begin(SpillSpec(self.max_bytes, media_type=media_type))
        try:
            handle.write(payload)
            artifact = handle.commit()
        except Exception:
            handle.abort()
            raise
        finally:
            handle.close()
        return SpillReference(artifact.sha256, text[: self.preview_chars], artifact.size_bytes, media_type, owner_id)

    def read(self, reference: SpillReference, *, max_bytes: int) -> bytes:
        if not isinstance(reference, SpillReference):
            raise TypeError("reference must be a SpillReference")
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        handle = self._find(reference.digest)
        if handle.size_bytes != reference.size:
            raise DurableSpillIntegrityError("spill reference size does not match stored artifact")
        return self.store.read(handle, max_bytes=max_bytes)

    def _find(self, digest: str) -> ArtifactHandle:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT spill_id,size_bytes,digest,media_type,name,state FROM execution_spill WHERE digest=?",
                (digest,),
            ).fetchone()
        if row is None or row[5] != SpillState.COMMITTED.value:
            raise DurableSpillIntegrityError("spill reference is not backed by a committed artifact")
        return ArtifactHandle(row[0], row[1], row[2], row[3], row[4])


__all__ = ["DurableExecutionOutput", "DurableSpillIntegrityError", "SQLiteSpillStore"]
