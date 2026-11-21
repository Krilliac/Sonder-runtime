"""Unified SQLite connection factory.

Replaces per-store ad-hoc sqlite3.connect() calls with a single factory
that enforces consistent PRAGMA settings, WAL mode, busy timeouts, and
parent directory creation.  Stores can migrate to this incrementally.

Thread-local caching is opt-in via ``cached_connection()`` for stores
that reuse a single connection per thread (fleet_store, autopilot_store,
composition_store).

No store behavior changes — this only standardizes the connection setup
that was duplicated across 16+ stores with inconsistent settings.
"""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect  # isort: skip

import logging
import os  # noqa: F401
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from typing import Protocol, TypedDict, Unpack

    class _OwnedSQLiteConnect(Protocol):
        def __call__(
            self,
            database: str,
            *,
            timeout: float = ...,
            check_same_thread: bool = ...,
        ) -> sqlite3.Connection: ...

    class _ConnectKwargs(TypedDict, total=False):
        timeout: float
        busy_timeout_ms: int
        wal: bool
        foreign_keys: bool
        row_factory: bool
        check_same_thread: bool

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5.0
_DEFAULT_BUSY_TIMEOUT_MS = 5000

_thread_local = threading.local()


class SQLiteConnectionSetupCleanupError(sqlite3.Error):
    """Failed setup left cleanup unresolved; reopening is not safe to retry."""

    retryable = False


def connect(
    db_path: str | Path,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS,
    wal: bool = True,
    foreign_keys: bool = False,
    row_factory: bool = True,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    path = str(db_path)

    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = cast("_OwnedSQLiteConnect", owned_sqlite_connect)(
        path, timeout=timeout, check_same_thread=check_same_thread,
    )

    try:
        if row_factory:
            conn.row_factory = sqlite3.Row

        conn.execute("PRAGMA busy_timeout=%d" % busy_timeout_ms)  # noqa: UP031

        if wal:
            conn.execute("PRAGMA journal_mode=WAL")

        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")
    except BaseException:
        try:
            conn.close()
        except BaseException as cleanup_error:
            # Never preserve a BUSY code when the failed handle may still be
            # open: a caller must not retry by opening another connection.
            raise SQLiteConnectionSetupCleanupError(
                "SQLite connection setup cleanup failed") from cleanup_error
        raise

    return conn


def cached_connection(
    cache_key: str,
    db_path: str | Path,
    *,
    schema_sql: str = "",
    **kwargs: Unpack[_ConnectKwargs],
) -> sqlite3.Connection:
    conn_attr = "_sqlite_factory_%s_conn" % cache_key  # noqa: UP031
    path_attr = "_sqlite_factory_%s_path" % cache_key  # noqa: UP031

    path = str(db_path)
    existing_conn: sqlite3.Connection | None = getattr(_thread_local, conn_attr, None)
    existing_path: str | None = getattr(_thread_local, path_attr, None)

    if existing_conn is not None and existing_path == path:
        return existing_conn

    if existing_conn is not None:
        try:
            existing_conn.close()
        except sqlite3.Error:
            pass

    conn = connect(db_path, **kwargs)

    if schema_sql:
        conn.executescript(schema_sql)

    setattr(_thread_local, conn_attr, conn)
    setattr(_thread_local, path_attr, path)
    return conn


def close_cached(cache_key: str) -> None:
    conn_attr = "_sqlite_factory_%s_conn" % cache_key  # noqa: UP031
    path_attr = "_sqlite_factory_%s_path" % cache_key  # noqa: UP031

    conn: sqlite3.Connection | None = getattr(_thread_local, conn_attr, None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        setattr(_thread_local, conn_attr, None)
        setattr(_thread_local, path_attr, None)


def close_current_thread() -> None:
    """Close actual cached handles without suppressing missing cleanup proof."""
    for name, connection in tuple(vars(_thread_local).items()):
        if name.startswith("_sqlite_factory_") and name.endswith("_conn") and connection is not None:
            connection.close()
            setattr(_thread_local, name, None)
            setattr(_thread_local, name[:-5] + "_path", None)


__all__ = [
    "SQLiteConnectionSetupCleanupError",
    "cached_connection",
    "close_cached",
    "connect",
]
