"""Deterministic context deduplication, snapshots, prefixes, and replay manifests."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable, Mapping


@dataclass(frozen=True)
class ContextItem:
    item_id: str
    section: str
    content: str
    provenance: str = ""
    protected: bool = False

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


@dataclass(frozen=True)
class ContextSnapshot:
    items: tuple[ContextItem, ...]
    token_count: int
    snapshot_digest: str


def deduplicate(items: Iterable[ContextItem]) -> tuple[ContextItem, ...]:
    seen: set[str] = set()
    result = []
    for item in items:
        if item.digest in seen:
            continue
        seen.add(item.digest)
        result.append(item)
    return tuple(result)


def snapshot(items: Iterable[ContextItem], *, token_count: int) -> ContextSnapshot:
    unique = deduplicate(items)
    raw = json.dumps([(item.item_id, item.section, item.digest, item.provenance) for item in unique], separators=(",", ":"))
    return ContextSnapshot(unique, token_count, hashlib.sha256(raw.encode()).hexdigest())


def prefix_manifest(items: Iterable[ContextItem]) -> tuple[str, ...]:
    return tuple(item.digest for item in deduplicate(items))


def replay_manifest(snapshot_value: ContextSnapshot, *, model: str, native_context: int) -> Mapping[str, object]:
    if native_context < 1 or not model:
        raise ValueError("model and native_context are required")
    return {"model": model, "native_context": native_context, "snapshot_digest": snapshot_value.snapshot_digest, "items": tuple(item.item_id for item in snapshot_value.items)}
