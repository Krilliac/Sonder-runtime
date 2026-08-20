"""Incremental, evidence-bound repository intelligence.

This module consumes parser/LSP/build-adapter records. It deliberately has no
filesystem or subprocess dependencies: discovery and persistence belong to
adapters, while this module provides deterministic application projections.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class FileEvidence:
    """Identity of the exact source version from which a record was read."""

    path: str
    sha256: str
    git_revision: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _text(self.path, "path").replace("\\", "/"))
        digest = _text(self.sha256, "sha256").lower()
        if not _SHA256.fullmatch(digest):
            raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "git_revision", self.git_revision.strip())


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    """A complete replacement record for one indexed symbol."""

    symbol_id: str
    name: str
    kind: str
    language: str
    evidence: FileEvidence
    line: int = 0
    signature: str = ""
    references: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    inheritance: tuple[str, ...] = ()
    calls: tuple[str, ...] = ()
    build_targets: tuple[str, ...] = ()
    token_cost: int = 0

    def __post_init__(self) -> None:
        for name in ("symbol_id", "name", "kind", "language"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if not isinstance(self.evidence, FileEvidence):
            raise TypeError("evidence must be FileEvidence")
        if not isinstance(self.line, int) or isinstance(self.line, bool) or self.line < 0:
            raise ValueError("line must be a non-negative integer")
        if not isinstance(self.token_cost, int) or isinstance(self.token_cost, bool) or self.token_cost < 0:
            raise ValueError("token_cost must be a non-negative integer")
        for name in ("references", "imports", "inheritance", "calls", "build_targets"):
            values = tuple(_text(value, name) for value in getattr(self, name))
            object.__setattr__(self, name, values)

    @property
    def file_path(self) -> str:
        return self.evidence.path

    @property
    def estimated_tokens(self) -> int:
        return self.token_cost or max(1, (len(self.signature or self.name) + 3) // 4)


@dataclass(frozen=True, slots=True)
class IndexDelta:
    """One parser/adaptor update; removed IDs are deleted atomically."""

    records: tuple[SymbolRecord, ...] = ()
    removed_symbol_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "removed_symbol_ids", tuple(_text(v, "symbol_id") for v in self.removed_symbol_ids))
        if any(not isinstance(record, SymbolRecord) for record in self.records):
            raise TypeError("records must contain SymbolRecord values")


@dataclass(frozen=True, slots=True)
class MapEntry:
    record: SymbolRecord
    score: float
    relation_hits: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RankedRepositoryMap:
    query: str
    token_budget: int
    entries: tuple[MapEntry, ...]
    total_tokens: int


class RepositoryIndex:
    """In-memory incremental projection; it never reads or writes repository files."""

    def __init__(self, records: Iterable[SymbolRecord] = ()) -> None:
        self._records: dict[str, SymbolRecord] = {}
        self._generation = 0
        self.apply(IndexDelta(tuple(records)))

    @property
    def generation(self) -> int:
        return self._generation

    def snapshot(self) -> Mapping[str, SymbolRecord]:
        return MappingProxyType(dict(self._records))

    def apply(self, delta: IndexDelta) -> int:
        """Apply a complete-record delta and return the new generation."""
        next_records = dict(self._records)
        for symbol_id in delta.removed_symbol_ids:
            next_records.pop(symbol_id, None)
        for record in delta.records:
            next_records[record.symbol_id] = record
        self._records = next_records
        self._generation += 1
        return self._generation

    def replace_file(self, evidence: FileEvidence, records: Iterable[SymbolRecord]) -> int:
        """Replace all records for one exact file version, without discovering it."""
        replacement = tuple(records)
        if any(record.evidence != evidence for record in replacement):
            raise ValueError("all replacement records must use the supplied file evidence")
        removed = tuple(record.symbol_id for record in self._records.values() if record.file_path == evidence.path)
        return self.apply(IndexDelta(replacement, removed))

    def ranked_map(self, query: str = "", *, token_budget: int = 2000) -> RankedRepositoryMap:
        if not isinstance(token_budget, int) or isinstance(token_budget, bool) or token_budget <= 0:
            raise ValueError("token_budget must be a positive integer")
        terms = tuple(dict.fromkeys(re.findall(r"[A-Za-z0-9_]+", query.casefold())))
        candidates = []
        for record in self._records.values():
            haystack = " ".join((record.name, record.kind, record.language, record.signature, *record.build_targets)).casefold()
            relation_text = " ".join((*record.references, *record.imports, *record.inheritance, *record.calls)).casefold()
            direct = sum(haystack.count(term) for term in terms)
            related = sum(relation_text.count(term) for term in terms)
            score = float(direct * 3 + related) if terms else 1.0
            hits = tuple(term for term in terms if term in relation_text)
            candidates.append(MapEntry(record, score, hits))
        candidates.sort(key=lambda entry: (-entry.score, entry.record.file_path, entry.record.line, entry.record.name, entry.record.symbol_id))
        selected = []
        used = 0
        for entry in candidates:
            cost = entry.record.estimated_tokens
            if used + cost > token_budget:
                continue
            selected.append(entry)
            used += cost
        return RankedRepositoryMap(query=query, token_budget=token_budget, entries=tuple(selected), total_tokens=used)


def digest_bytes(content: bytes) -> str:
    """Pure helper for adapters that already own file reading."""
    return hashlib.sha256(content).hexdigest()
