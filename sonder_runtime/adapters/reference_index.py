"""Read-only FTS5 store for the reference lookup.

The index is built elsewhere; this adapter only reads it.  Every query uses a
fresh, short-lived connection opened through a ``file:`` URI with
``mode=ro`` and ``PRAGMA query_only=ON``.  It never writes, never ATTACHes
another database and never opens the ``path`` column stored with a passage
(that value is only text that is shown as a source label).  The validation
result is cached by (path, mtime, size) so a changed file is re-checked.

The connection is made through the runtime's owned-SQLite factory, like every
other handle the runtime opens.  Two consequences: on POSIX the factory
removes group/other permission bits from the index file when the runtime's
user owns it (the file's content is never touched); and in a managed child
process, whose owner admits only paths inside its private roots, an index
outside them is refused and the lookup reports itself unavailable.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect
from sonder_runtime.domain.reference_lookup.models import (
    IndexState,
    IndexStatus,
    RankedRow,
    ReferenceIndexError,
)
from sonder_runtime.domain.reference_lookup.sanitize import sanitize
from sonder_runtime.domain.reference_lookup.selection import priority_rows

REQUIRED_COLUMNS = frozenset({"title", "heading", "body", "source", "url", "path", "doc"})
BUSY_TIMEOUT_SECONDS = 5.0
TITLE_TOPUP_LIMIT = 50

_SEARCH_COLUMNS = ("title, heading, body, source, url, path, doc, "
                   "bm25(passages, 4.0, 2.0, 1.0) AS rank")
_SEARCH_SQL = (f"SELECT {_SEARCH_COLUMNS} FROM passages "
               "WHERE passages MATCH ? ORDER BY rank LIMIT ?")
_TITLE_SQL = (f"SELECT {_SEARCH_COLUMNS} FROM passages WHERE passages MATCH ? "
              f"ORDER BY rank LIMIT {TITLE_TOPUP_LIMIT}")

_CacheKey = tuple[str, int, int]


def classify_error(exc: BaseException) -> IndexState:
    """Map a sqlite error to the index state it means."""
    text = str(exc).lower()
    if "no such module" in text:
        return IndexState.FTS5_UNAVAILABLE
    if "no such table" in text or "no such column" in text:
        return IndexState.WRONG_SCHEMA
    if "unable to open" in text:
        return IndexState.UNREADABLE
    if "not a database" in text or "malformed" in text:
        return IndexState.NOT_SQLITE
    return IndexState.ERROR


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _row(values: Sequence[object]) -> RankedRow:
    return RankedRow(
        title=_text(values[0]), heading=_text(values[1]), body=_text(values[2]),
        source=_text(values[3]), url=_text(values[4]), path=_text(values[5]),
        doc=values[6],  # type: ignore[arg-type]  # opaque, only compared and hashed
        rank=float(values[7]),  # type: ignore[arg-type]
    )


class ReferenceIndex:
    """One reference index file: validated status plus read-only searches.

    Thread-safe: the only shared state is the status cache, guarded by a lock;
    each search opens and closes its own connection.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._cached: tuple[_CacheKey, IndexStatus] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A read-only connection that is closed on exit."""
        uri = self._path.resolve().as_uri() + "?mode=ro"
        try:
            conn = owned_sqlite_connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)
        except RuntimeError as exc:
            # A managed process's owner refused the handle (path outside its
            # roots, admission stopped or at capacity): unavailable, not fatal.
            raise sqlite3.OperationalError("index not admitted by the runtime: %s" % exc) from None
        try:
            conn.execute("PRAGMA query_only=ON")
            yield conn
        finally:
            conn.close()

    def status(self) -> IndexStatus:
        """Validate the index (cached by path, mtime and size); never raises."""
        path_text = str(self._path)
        try:
            info = self._path.stat()
        except FileNotFoundError:
            return IndexStatus(IndexState.MISSING, path_text, detail="file not found")
        except OSError as exc:
            return IndexStatus(IndexState.UNREADABLE, path_text,
                               detail=sanitize(exc.strerror or type(exc).__name__))
        if not stat.S_ISREG(info.st_mode):
            return IndexStatus(IndexState.UNREADABLE, path_text, detail="not a regular file")
        key: _CacheKey = (path_text, info.st_mtime_ns, info.st_size)
        with self._lock:
            if self._cached is not None and self._cached[0] == key:
                return self._cached[1]
        status = self._probe(path_text)
        if status.state is not IndexState.ERROR:   # a transient error is not remembered
            with self._lock:
                self._cached = (key, status)
        return status

    def search_rows(self, match: str, limit: int, idents: Sequence[str] = ()) -> tuple[RankedRow, ...]:
        """Ranked rows for an FTS5 MATCH expression, identifier-title rows first.

        Raises ReferenceIndexError when the index is unusable or a query fails.
        """
        status = self.status()
        if not status.ready:
            raise ReferenceIndexError(status.state, status.detail)
        if not match or limit <= 0:
            return ()
        try:
            with self.connect() as conn:
                rows = [_row(r) for r in conn.execute(_SEARCH_SQL, (match, limit))]
                if idents:
                    def fetch(ident: str) -> list[RankedRow]:
                        phrase = 'title : "' + ident.replace('"', '""') + '"'
                        return [_row(r) for r in conn.execute(_TITLE_SQL, (phrase,))]

                    rows = priority_rows(rows, idents, fetch) + rows
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise ReferenceIndexError(classify_error(exc), sanitize(exc)) from None
        return tuple(rows)

    def _probe(self, path_text: str) -> IndexStatus:
        try:
            with self.connect() as conn:
                return self._validate(conn, path_text)
        except (sqlite3.Error, OSError, ValueError) as exc:
            return IndexStatus(classify_error(exc), path_text, detail=sanitize(exc))

    @staticmethod
    def _validate(conn: sqlite3.Connection, path_text: str) -> IndexStatus:
        def wrong(detail: str) -> IndexStatus:
            return IndexStatus(IndexState.WRONG_SCHEMA, path_text, detail=detail)

        tables = dict(conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN ('passages', 'meta')").fetchall())
        if "passages" not in tables:
            return wrong("no passages table")
        if "meta" not in tables:
            return wrong("no meta table")
        if "fts5" not in str(tables["passages"]).lower():
            return wrong("passages is not an FTS5 table")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(passages)")}
        if not REQUIRED_COLUMNS <= columns:
            return wrong("passages lacks columns: " + ", ".join(sorted(REQUIRED_COLUMNS - columns)))
        conn.execute("SELECT rowid FROM passages LIMIT 1").fetchall()   # loads the FTS5 module
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        try:
            summary = json.loads(meta["summary"])
        except (KeyError, TypeError, ValueError):
            return wrong("meta has no readable summary row")
        if not isinstance(summary, dict):
            return wrong("meta summary is not an object")
        try:
            passages = int(summary.get("passages", 0))
            by_source = summary.get("by_source", {})
            sources = tuple(sorted((str(k), int(v)) for k, v in by_source.items()))
        except (TypeError, ValueError, AttributeError):
            return wrong("meta summary has unreadable counts")
        return IndexStatus(IndexState.READY, path_text, passages=passages, sources=sources,
                           built_at=sanitize(_text(meta.get("built_at"))))
