"""Deterministic context manifests (WP4 CTX-004/006/009/010).

This module is an integration-neutral boundary.  It does not call the context
planner, providers, embedders, or persistence adapters.  Producers supply
immutable records and receive immutable, hashable manifests with provenance.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import string
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _semantic_tokens(text: str) -> tuple[str, ...]:
    folded = text.casefold().translate(str.maketrans({char: " " for char in string.punctuation}))
    return tuple(re.findall(r"\w+", folded, flags=re.UNICODE))


@dataclass(frozen=True)
class ContextRecord:
    """A rendered context item and its producer provenance."""

    item_id: str
    section: str
    content: str
    source: str
    ordinal: int = 0
    stable: bool = False

    def __post_init__(self) -> None:
        if not self.item_id or not self.section or not self.source:
            raise ValueError("item_id, section, and source must be non-empty")
        if not isinstance(self.content, str):
            raise TypeError("content must be a string")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("ordinal must be a non-negative integer")

    @property
    def content_digest(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def semantic_key(self) -> tuple[str, ...]:
        return _semantic_tokens(self.content)


@dataclass(frozen=True)
class DedupProvenance:
    """Why an item was removed and which item was retained."""

    removed_item_id: str
    retained_item_id: str
    reason: str
    similarity: float
    removed_source: str
    retained_source: str


@dataclass(frozen=True)
class DeduplicationResult:
    retained: tuple[ContextRecord, ...]
    removed: tuple[ContextRecord, ...]
    provenance: tuple[DedupProvenance, ...]


def _similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    a, b = Counter(left), Counter(right)
    overlap = sum((a & b).values())
    return (2.0 * overlap) / (sum(a.values()) + sum(b.values()))


def deduplicate_context(
    records: Sequence[ContextRecord], *, semantic_threshold: float = 0.90
) -> DeduplicationResult:
    """Remove exact, then semantic, duplicates in deterministic producer order.

    First occurrence wins (ordinal, then item id), so provenance is stable even
    when callers provide records in a different order.  Exact comparison keeps
    line endings canonical but otherwise preserves content; semantic comparison
    is a bounded token Dice similarity and is intentionally explainable.
    """
    if not 0.0 <= semantic_threshold <= 1.0:
        raise ValueError("semantic_threshold must be between 0 and 1")
    ordered = tuple(sorted(records, key=lambda item: (item.ordinal, item.item_id)))
    if any(not isinstance(item, ContextRecord) for item in ordered):
        raise TypeError("records must contain ContextRecord values")
    retained: list[ContextRecord] = []
    removed: list[ContextRecord] = []
    provenance: list[DedupProvenance] = []
    exact: dict[str, ContextRecord] = {}
    for record in ordered:
        exact_key = record.content.replace("\r\n", "\n").replace("\r", "\n")
        winner = exact.get(exact_key)
        reason = "exact"
        similarity = 1.0
        if winner is None:
            for candidate in retained:
                score = _similarity(record.semantic_key, candidate.semantic_key)
                if score >= semantic_threshold:
                    winner, similarity, reason = candidate, score, "semantic"
                    break
        if winner is not None:
            removed.append(record)
            provenance.append(DedupProvenance(record.item_id, winner.item_id, reason, similarity, record.source, winner.source))
            continue
        exact[exact_key] = record
        retained.append(record)
    return DeduplicationResult(tuple(retained), tuple(removed), tuple(provenance))


@dataclass(frozen=True)
class Snapshot:
    revision: int
    value: Any
    digest: str


class LastGoodSnapshot:
    """Retains the last complete valid view and never publishes partial data."""

    def __init__(self) -> None:
        self._snapshot: Snapshot | None = None

    def publish(self, value: Any, *, complete: bool = True, validator: Callable[[Any], bool] | None = None) -> Snapshot | None:
        candidate = copy.deepcopy(value)
        if not complete or (validator is not None and not validator(candidate)):
            return self.get()
        revision = 1 if self._snapshot is None else self._snapshot.revision + 1
        snapshot = Snapshot(revision, candidate, _digest(candidate))
        self._snapshot = snapshot
        return self.get()

    def get(self) -> Snapshot | None:
        if self._snapshot is None:
            return None
        return Snapshot(self._snapshot.revision, copy.deepcopy(self._snapshot.value), self._snapshot.digest)


@dataclass(frozen=True)
class PrefixManifest:
    version: str
    sections: tuple[ContextRecord, ...]
    cache_key: str


def build_prefix_manifest(records: Sequence[ContextRecord], *, version: str = "1") -> PrefixManifest:
    """Build a stable prefix from stable records, independent of input order."""
    if not version:
        raise ValueError("version must be non-empty")
    stable = tuple(record for record in records if record.stable)
    ordered = tuple(sorted(stable, key=lambda item: (item.section, item.item_id, item.content_digest)))
    material = {"version": version, "sections": [(item.section, item.item_id, item.content_digest) for item in ordered]}
    return PrefixManifest(version, ordered, _digest(material))


class PrefixManifestCache:
    """Small deterministic cache accounting prefix hits and writes."""

    def __init__(self) -> None:
        self._values: dict[str, PrefixManifest] = {}
        self.hits = 0
        self.writes = 0

    def resolve(self, records: Sequence[ContextRecord], *, version: str = "1") -> PrefixManifest:
        manifest = build_prefix_manifest(records, version=version)
        cached = self._values.get(manifest.cache_key)
        if cached is not None:
            self.hits += 1
            return cached
        self._values[manifest.cache_key] = manifest
        self.writes += 1
        return manifest


@dataclass(frozen=True)
class ReplaySection:
    item_id: str
    section: str
    content_digest: str
    source: str
    ordinal: int


@dataclass(frozen=True)
class ReplayManifest:
    request_id: str
    model: str
    sections: tuple[ReplaySection, ...]
    prefix_key: str
    metadata: Mapping[str, Any]
    manifest_digest: str


def build_replay_manifest(
    request_id: str, model: str, records: Sequence[ContextRecord], *, prefix_key: str = "", metadata: Mapping[str, Any] | None = None
) -> ReplayManifest:
    """Capture the ordered, exact section inputs needed to reproduce a request."""
    if not request_id or not model:
        raise ValueError("request_id and model must be non-empty")
    if len({record.item_id for record in records}) != len(tuple(records)):
        raise ValueError("replay records must have unique item_id values")
    sections = tuple(ReplaySection(r.item_id, r.section, r.content_digest, r.source, r.ordinal) for r in records)
    safe_metadata = copy.deepcopy(dict(metadata or {}))
    material = {"request_id": request_id, "model": model, "sections": [section.__dict__ for section in sections], "prefix_key": prefix_key, "metadata": safe_metadata}
    return ReplayManifest(request_id, model, sections, prefix_key, MappingProxyType(safe_metadata), _digest(material))


__all__ = [
    "ContextRecord", "DedupProvenance", "DeduplicationResult", "deduplicate_context",
    "Snapshot", "LastGoodSnapshot", "PrefixManifest", "PrefixManifestCache",
    "ReplaySection", "ReplayManifest", "build_prefix_manifest", "build_replay_manifest",
]
