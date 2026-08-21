"""SQLite persistence adapter for extension registry state."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Sequence

from ....application.extensions.quarantine import QuarantineDecision
from ....application.extensions.provenance_inventory import ExtensionHealthState
from ....application.extensions.registry import ExtensionInstallRecord, ExtensionScope
from ....domain.extensions.manifest import (
    CleanupPolicy, ExtensionDependency, ExtensionHealth, ExtensionIdentity,
    ExtensionManifest, HealthMode,
)


_DDL = """CREATE TABLE IF NOT EXISTS extension_registry_state (
    slot TEXT PRIMARY KEY, record_json TEXT NOT NULL
)"""


def _manifest(value: dict[str, Any]) -> ExtensionManifest:
    return ExtensionManifest(
        ExtensionIdentity(value["identity"]["name"], value["identity"]["publisher"]),
        value["version"], value["protocol"],
        tuple(ExtensionDependency(x["name"], x["version"], x["required"]) for x in value["dependencies"]),
        tuple(value["permissions"]),
        ExtensionHealth(HealthMode(value["health"]["mode"]), value["health"]["crash_limit"], value["health"]["probe_timeout_ms"]),
        CleanupPolicy(value["cleanup"]["on_quarantine"], value["cleanup"]["retain_state"]),
    )


def _record(record: ExtensionInstallRecord) -> dict[str, Any]:
    manifest = record.manifest
    return {
        "extension_id": record.extension_id, "scope": record.scope.value, "project_id": record.project_id,
        "version": record.version, "manifest_digest": record.manifest_digest, "enabled": record.enabled,
        "health_state": record.health_state.value, "health_reasons": list(record.health_reasons),
        "crash_count": record.crash_count,
        "manifest": {"identity": {"name": manifest.identity.name, "publisher": manifest.identity.publisher},
                     "version": manifest.version, "protocol": manifest.protocol,
                     "dependencies": [{"name": x.name, "version": x.version, "required": x.required} for x in manifest.dependencies],
                     "permissions": list(manifest.permissions),
                     "health": {"mode": manifest.health.mode.value, "crash_limit": manifest.health.crash_limit, "probe_timeout_ms": manifest.health.probe_timeout_ms},
                     "cleanup": {"on_quarantine": manifest.cleanup.on_quarantine, "retain_state": manifest.cleanup.retain_state}},
        "quarantine": None if record.quarantine is None else {
            "extension_id": record.quarantine.extension_id, "quarantined": record.quarantine.quarantined,
            "reasons": list(record.quarantine.reasons), "cleanup_action": record.quarantine.cleanup_action,
            "retain_state": record.quarantine.retain_state,
        },
    }


def _decode(value: str) -> ExtensionInstallRecord:
    data = json.loads(value)
    quarantine = data["quarantine"]
    decision = None if quarantine is None else QuarantineDecision(
        quarantine["extension_id"], quarantine["quarantined"], tuple(quarantine["reasons"]),
        quarantine["cleanup_action"], quarantine["retain_state"],
    )
    manifest = _manifest(data["manifest"])
    return ExtensionInstallRecord(
        data["extension_id"], ExtensionScope(data["scope"]), data["project_id"], data["version"],
        data["manifest_digest"], manifest, data["enabled"], ExtensionHealthState(data["health_state"]),
        tuple(data["health_reasons"]), decision, data["crash_count"],
    )


class SQLiteExtensionStateRepository:
    """Bounded, transactional state store with fail-closed row validation."""

    def __init__(self, db_path: str | Path, *, max_records: int = 256) -> None:
        if max_records < 1 or max_records > 4096:
            raise ValueError("max_records must be between 1 and 4096")
        self._path, self._max_records = Path(db_path), max_records
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self._path)) as connection:
            connection.execute(_DDL)

    def load(self) -> tuple[ExtensionInstallRecord, ...]:
        with sqlite3.connect(str(self._path)) as connection:
            rows = connection.execute("SELECT record_json FROM extension_registry_state ORDER BY slot").fetchall()
        records = tuple(_decode(row[0]) for row in rows)
        if len(records) > self._max_records or len({record.key for record in records}) != len(records):
            raise ValueError("extension state is invalid or exceeds capacity")
        for record in records:
            if record.manifest.digest() != record.manifest_digest or record.manifest.extension_id != record.extension_id:
                raise ValueError("extension state manifest digest mismatch")
        return records

    def save(self, records: Sequence[ExtensionInstallRecord]) -> None:
        if len(records) > self._max_records or len({record.key for record in records}) != len(records):
            raise ValueError("extension state exceeds capacity")
        encoded = [(record.key[0] + ":" + record.key[1] + ":" + record.key[2], json.dumps(_record(record), sort_keys=True, separators=(",", ":"))) for record in records]
        with sqlite3.connect(str(self._path)) as connection:
            connection.execute("DELETE FROM extension_registry_state")
            connection.executemany("INSERT INTO extension_registry_state(slot, record_json) VALUES (?, ?)", encoded)


__all__ = ["SQLiteExtensionStateRepository"]
