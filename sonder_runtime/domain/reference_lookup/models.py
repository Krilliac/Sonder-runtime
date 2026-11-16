"""Typed values shared by the reference-lookup modules."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IndexState(str, Enum):
    """Why a reference index can or cannot be used."""

    READY = "ready"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    NOT_SQLITE = "not_sqlite"
    WRONG_SCHEMA = "wrong_schema"
    FTS5_UNAVAILABLE = "fts5_unavailable"
    ERROR = "error"


class SchemaCompat(str, Enum):
    """How the index's ``schema_version`` relates to what this runtime reads.

    ``NEWER`` is degraded but usable: the table shape this runtime validates
    is intact, so passages are searched, but the newer ``sources`` metadata
    is not interpreted (the search-terms prompt stays generic).
    """

    LEGACY = "legacy"      # no schema_version: version 1
    CURRENT = "current"    # schema 2: subjects are read
    NEWER = "newer"        # built by a newer library tool
    INVALID = "invalid"    # schema_version present but unreadable: treated as legacy


@dataclass(frozen=True)
class LibrarySource:
    """One entry of the index's ``sources`` metadata, already sanitised and capped."""

    source: str
    subject: str
    passages: int
    licence: str


@dataclass(frozen=True)
class IndexStatus:
    """Result of validating the index file; ``detail`` is one sanitised line.

    ``schema_version`` is the version the index declares (1 when it declares
    none); ``library`` the usable ``sources`` entries; ``warnings`` one
    sanitised line per ignored or unusable piece of metadata.
    """

    state: IndexState
    path: str
    passages: int = 0
    sources: tuple[tuple[str, int], ...] = ()
    built_at: str = ""
    detail: str = ""
    schema_version: int = 1
    schema_compat: SchemaCompat = SchemaCompat.LEGACY
    library: tuple[LibrarySource, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def subjects(self) -> tuple[str, ...]:
        """The distinct subjects of the library, in index order (empty for a version 1 index)."""
        return tuple(dict.fromkeys(entry.subject for entry in self.library))

    @property
    def ready(self) -> bool:
        return self.state is IndexState.READY


class ReferenceIndexError(Exception):
    """The index could not be searched; ``state`` says why."""

    def __init__(self, state: IndexState, detail: str = "") -> None:
        super().__init__(f"{state.value}: {detail}" if detail else state.value)
        self.state = state
        self.detail = detail


@dataclass(frozen=True)
class RankedRow:
    """One FTS5 hit as stored; ``rank`` is the raw bm25 value (lower is better)."""

    title: str
    heading: str
    body: str
    source: str
    url: str
    path: str
    doc: int
    rank: float
    rowid: int = 0


@dataclass(frozen=True)
class Passage:
    """A selected passage; its number in the prompt is its 1-based position.

    ``rowid`` is the passage's row in the index (0 when unknown); it is only
    used to ask the index which search terms the passage holds.
    """

    title: str
    heading: str
    source: str
    url: str
    path: str
    text: str
    score: float = 0.0
    rowid: int = 0


@dataclass(frozen=True)
class TermStats:
    """What the index knows about a query's terms.

    ``passage_count`` is the size of the index, ``matching`` the number of
    passages holding each term and ``held`` the terms each asked-about row holds.
    """

    passage_count: int
    matching: dict[str, int]
    held: dict[int, frozenset[str]]
