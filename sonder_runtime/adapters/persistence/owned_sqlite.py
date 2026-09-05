"""Opt-in, exact SQLite handles owned by one disposable runtime process.

Existing callers retain native sqlite3 behavior until private host composition
installs an owner. This is an explicit factory, never a sqlite3 monkeypatch.
Thread-affine handles are closed on their constructing thread only.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from threading import RLock, current_thread
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class SQLiteOwnershipSnapshot:
    open_handles: int
    constructing: int
    unresolved: bool

    @property
    def clean(self):
        return not self.open_handles and not self.constructing and not self.unresolved


class _OwnedConnection(sqlite3.Connection):
    def close(self):
        owner = getattr(self, "_owned_registry", None)
        if owner is None:
            return super().close()
        owner._close(self)


class OwnedSQLiteConnections:
    def __init__(self, private_roots, *, max_connections=256, validate=lambda: None):
        if type(private_roots) is not tuple or not 1 <= len(private_roots) <= 16:
            raise ValueError("bounded private SQLite roots required")
        if type(max_connections) is not int or not 1 <= max_connections <= 256:
            raise ValueError("bounded SQLite connection capacity required")
        self._roots = tuple(Path(item).resolve() for item in private_roots)
        self._validate = validate
        self._maximum = max_connections
        self._lock = RLock()
        self._records = {}
        self._constructing = 0
        self._unresolved = False
        self._stopped = False

    def _path(self, database, uri):
        value = os.fsdecode(database)
        if len(value) > 4096:
            raise RuntimeError("SQLite path exceeds ownership bounds")
        if value == ":memory:":
            return
        if uri:
            parsed = urlsplit(value)
            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.fragment:
                raise RuntimeError("unowned SQLite URI")
            value = unquote(parsed.path, errors="strict")
            if os.name == "nt" and len(value) > 3 and value[0] == "/" and value[2] == ":":
                value = value[1:]
        path = Path(value).resolve()
        if not any(path.is_relative_to(root) for root in self._roots):
            raise RuntimeError("SQLite path is outside the owned namespace")

    def connect(self, database, *args, **kwargs):
        # The managed boundary uses explicit keyword configuration; caller
        # supplied factories could conceal additional handles and are refused.
        if args or "factory" in kwargs:
            raise RuntimeError("managed SQLite requires the fixed connection factory")
        self._validate()
        self._path(database, kwargs.get("uri", False))
        with self._lock:
            if self._stopped or len(self._records) + self._constructing >= self._maximum:
                raise RuntimeError("owned SQLite admission stopped or at capacity")
            self._constructing += 1
        connection = None
        try:
            connection = sqlite3.connect(database, factory=_OwnedConnection, **kwargs)
            with self._lock:
                connection._owned_registry = self
                self._records[id(connection)] = (connection, current_thread())
                stopped = self._stopped
            if stopped:
                connection.close()
                raise RuntimeError("owned SQLite admission stopped during construction")
            return connection
        finally:
            with self._lock:
                self._constructing -= 1

    def _close(self, connection):
        with self._lock:
            record = self._records.get(id(connection))
            if record is None:
                return
            if record[0] is not connection or record[1] is not current_thread():
                raise RuntimeError("SQLite close requires the exact constructing thread")
            # Explicitly abandoned transactions are cleaned up, but never
            # reported as a fully acknowledged Application shutdown.
            pending = connection.in_transaction
            try:
                sqlite3.Connection.close(connection)
            except BaseException:
                self._unresolved = True
                raise
            self._records.pop(id(connection))
            self._unresolved |= pending

    def stop_admissions(self):
        with self._lock:
            self._stopped = True

    def snapshot(self):
        with self._lock:
            return SQLiteOwnershipSnapshot(len(self._records), self._constructing, self._unresolved)

    def close_current_thread(self):
        with self._lock:
            connections = tuple(record[0] for record in self._records.values() if record[1] is current_thread())
        for connection in connections:
            try:
                connection.close()
            except BaseException:
                with self._lock:
                    self._unresolved = True
        return self.snapshot()


_PROCESS_OWNER = None


def install_disposable_owner(owner):
    """Private child startup only, before any runtime store construction.

    Installation is terminal for this process: no reset/replacement API can
    reopen stopped admission or adopt an existing Application's connections.
    """
    global _PROCESS_OWNER
    if type(owner) is not OwnedSQLiteConnections or _PROCESS_OWNER is not None:
        raise RuntimeError("one exact disposable SQLite owner required")
    _PROCESS_OWNER = owner


def connect(*args, **kwargs):
    owner = _PROCESS_OWNER
    if owner is None:
        return sqlite3.connect(*args, **kwargs)
    return owner.connect(*args, **kwargs)


@contextmanager
def transaction(*args, **kwargs):
    """Commit/rollback the operation, then close its exact connection."""
    connection = connect(*args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()
