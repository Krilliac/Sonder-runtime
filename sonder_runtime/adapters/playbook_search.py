"""Write-time lexical candidate retrieval using the existing lesson FTS path."""
from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar, cast

from sonder_runtime.adapters.memory_store import fts_search
from sonder_runtime.adapters.persistence.owned_sqlite import connect

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping
    from typing import Protocol

    class _FtsSearch(Protocol):
        def __call__(self, conn: sqlite3.Connection, query: str, limit: int = ...) -> list[str]: ...


_EntryT = TypeVar("_EntryT", bound="Mapping[str, object]")


def duplicate_candidates(query: str, entries: Iterable[_EntryT]) -> list[_EntryT]:
    """Select at most 128 candidates without embeddings, models or disk writes.

    Exact duplicate detection still examines all entries before this helper.
    FTS is a candidate generator, not a claim that two notes are equivalent.
    """
    bounded = list(entries)[:512]
    connection = cast("Callable[[str], sqlite3.Connection]", connect)(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE lessons_fts USING fts5(lesson_id UNINDEXED, text)")
        connection.executemany(
            "INSERT INTO lessons_fts(lesson_id,text) VALUES(?,?)",
            [(str(index), " ".join(str(row.get(key) or "") for key in ("title", "body", "evidence")))
             for index, row in enumerate(bounded)],
        )
        return [
            bounded[int(index)]
            for index in cast("_FtsSearch", fts_search)(connection, query, limit=128)
        ]
    finally:
        connection.close()
