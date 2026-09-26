"""SQLite store for the procedural skill publication catalog snapshot.

The catalog is kept as one generation-counted row holding the canonical JSON
of a ``CatalogSnapshot`` and its integrity digest.  ``save`` verifies the
snapshot before writing and replaces the row inside one ``BEGIN IMMEDIATE``
transaction; ``load`` rebuilds the snapshot and verifies it through
``DurableLastGoodCatalog.from_snapshot`` so a tampered or malformed row fails
closed.  The digest detects corruption and uncoordinated edits; it is not an
authenticity signature against a writer who can recompute SHA-256.

Writers are serialized by the stored generation: an instance remembers the
generation it last loaded or saved, and ``save`` refuses, inside the same
``BEGIN IMMEDIATE`` transaction, when another instance or process has written
since.  The refused writer's service rolls back and must reload.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
from threading import Lock
from typing import Any, Iterator
import sqlite3

from sonder_runtime.adapters.persistence.owned_sqlite import (
    transaction as owned_sqlite_transaction,
)
from sonder_runtime.application.skills.procedural_publication import (
    CatalogSnapshot,
    DurableLastGoodCatalog,
    PublicationError,
    PublicationState,
    SkillPublication,
)

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS procedural_skill_catalog (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_version INTEGER NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 1),
    snapshot_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""

_PUBLICATION_FIELDS = frozenset(SkillPublication.__dataclass_fields__)


class CatalogStoreError(PublicationError):
    """Raised when the stored catalog cannot be trusted or written."""


def _publication_payload(publication: SkillPublication) -> dict[str, Any]:
    return {
        **publication.__dict__,
        "state": publication.state.value,
        "source_interaction_ids": list(publication.source_interaction_ids),
        "published_at": publication.published_at.isoformat(),
    }


def _publication(value: Any) -> SkillPublication:
    if not isinstance(value, dict) or set(value) != _PUBLICATION_FIELDS:
        raise CatalogStoreError("stored catalog revision has an unexpected shape")
    ids = value["source_interaction_ids"]
    if not isinstance(ids, list):
        raise CatalogStoreError("stored catalog revision provenance is malformed")
    published_at = datetime.fromisoformat(value["published_at"])
    if published_at.tzinfo is None:
        raise CatalogStoreError("stored catalog revision timestamp is not timezone-aware")
    return SkillPublication(**{
        **value,
        "state": PublicationState(value["state"]),
        "source_interaction_ids": tuple(ids),
        "published_at": published_at,
    })


def _pairs(value: Any, name: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise CatalogStoreError(f"stored catalog {name} index is malformed")
    pairs = []
    for item in value:
        if (not isinstance(item, list) or len(item) != 2
                or not all(isinstance(part, str) for part in item)):
            raise CatalogStoreError(f"stored catalog {name} index is malformed")
        pairs.append((item[0], item[1]))
    return tuple(pairs)


def _encode(snapshot: CatalogSnapshot) -> str:
    payload = {
        "revisions": [_publication_payload(item) for item in snapshot.revisions],
        "active": [list(item) for item in snapshot.active],
        "last_good": [list(item) for item in snapshot.last_good],
        "disabled": [list(item) for item in snapshot.disabled],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode(payload_json: str, digest: str) -> CatalogSnapshot:
    try:
        payload = json.loads(payload_json)
        if not isinstance(payload, dict) or set(payload) != {"revisions", "active", "last_good", "disabled"}:
            raise CatalogStoreError("stored catalog payload has an unexpected shape")
        revisions = payload["revisions"]
        if not isinstance(revisions, list):
            raise CatalogStoreError("stored catalog revisions are malformed")
        snapshot = CatalogSnapshot(
            tuple(_publication(item) for item in revisions),
            _pairs(payload["active"], "active"),
            _pairs(payload["last_good"], "last_good"),
            _pairs(payload["disabled"], "disabled"),
            digest,
        )
    except CatalogStoreError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise CatalogStoreError("stored procedural skill catalog failed verification") from exc
    _verify(snapshot, "stored procedural skill catalog failed verification")
    return snapshot


def _verify(snapshot: CatalogSnapshot, failure: str) -> None:
    """Recompute the canonical digest and resolve every index entry."""
    if not isinstance(snapshot, CatalogSnapshot):
        raise TypeError("a CatalogSnapshot is required")
    try:
        restored = DurableLastGoodCatalog.from_snapshot(snapshot)
        for skill_id, _version in snapshot.active:
            restored.current(skill_id)
        for skill_id, _version in snapshot.last_good:
            restored.last_good(skill_id)
    except (PublicationError, ValueError, TypeError) as exc:
        raise CatalogStoreError(failure) from exc


class SQLiteCatalogSnapshotStore:
    """Single-row, generation-counted ``CatalogStorePort`` over SQLite."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        # Generation this instance last loaded or saved; 0 is the empty store.
        self._generation = 0
        with self._connect() as connection:
            connection.executescript(_DDL)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with owned_sqlite_transaction(str(self._path), timeout=5.0) as connection:
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection

    def generation(self) -> int:
        """Return the stored generation, or 0 when nothing was saved yet."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT generation FROM procedural_skill_catalog WHERE singleton=1"
            ).fetchone()
        return 0 if row is None else int(row[0])

    def load(self) -> CatalogSnapshot | None:
        """Return the verified snapshot and adopt its generation for saves."""
        with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT schema_version,generation,snapshot_digest,payload_json "
                    "FROM procedural_skill_catalog WHERE singleton=1"
                ).fetchone()
            if row is None:
                self._generation = 0
                return None
            schema_version, generation, digest, payload_json = row
            if schema_version != SCHEMA_VERSION:
                raise CatalogStoreError("stored catalog schema version is not supported")
            if (type(generation) is not int or generation < 1
                    or not isinstance(digest, str) or not isinstance(payload_json, str)):
                raise CatalogStoreError("stored procedural skill catalog failed verification")
            snapshot = _decode(payload_json, digest)
            self._generation = generation
            return snapshot

    def save(self, snapshot: CatalogSnapshot) -> None:
        """Replace the row, refusing when it changed since this instance read it."""
        _verify(snapshot, "refusing to persist an unverified catalog snapshot")
        payload_json = _encode(snapshot)
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT generation FROM procedural_skill_catalog WHERE singleton=1"
                ).fetchone()
                stored = 0 if row is None else int(row[0])
                if stored != self._generation:
                    raise CatalogStoreError(
                        "procedural skill catalog changed since it was loaded"
                    )
                generation = stored + 1
                connection.execute(
                    "INSERT INTO procedural_skill_catalog"
                    "(singleton,schema_version,generation,snapshot_digest,payload_json) "
                    "VALUES (1,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                    "schema_version=excluded.schema_version,generation=excluded.generation,"
                    "snapshot_digest=excluded.snapshot_digest,payload_json=excluded.payload_json",
                    (SCHEMA_VERSION, generation, snapshot.snapshot_digest, payload_json),
                )
            self._generation = generation


__all__ = ["CatalogStoreError", "SQLiteCatalogSnapshotStore", "SCHEMA_VERSION"]
