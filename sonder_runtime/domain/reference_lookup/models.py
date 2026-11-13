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


@dataclass(frozen=True)
class IndexStatus:
    """Result of validating the index file; ``detail`` is one sanitised line."""

    state: IndexState
    path: str
    passages: int = 0
    sources: tuple[tuple[str, int], ...] = ()
    built_at: str = ""
    detail: str = ""

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


@dataclass(frozen=True)
class Passage:
    """A selected passage; its number in the prompt is its 1-based position."""

    title: str
    heading: str
    source: str
    url: str
    path: str
    text: str
    score: float = 0.0
