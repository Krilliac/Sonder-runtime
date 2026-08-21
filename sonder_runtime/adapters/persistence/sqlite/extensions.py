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


def _validate_record(record: ExtensionInstallRecord) -> None:
    """Validate every duplicated/derived field before admitting a row."""
    if record.extension_id != record.manifest.extension_id:
        raise ValueError("extension state identity mismatch")
    if record.version != record.manifest.version:
        raise ValueError("extension state version mismatch")
    if record.manifest_digest != record.manifest.digest():
        raise ValueError("extension state manifest digest mismatch")
    if record.scope.value == "global" and record.project_id is not None:
        raise ValueError("global extension state cannot have a project_id")
    if record.scope.value == "project" and (
        not isinstance(record.project_id, str) or not record.project_id.strip()
    ):
        raise ValueError("project extension state requires a project_id")
    if not isinstance(record.enabled, bool) or not isinstance(record.crash_count, int) or record.crash_count < 0:
        raise ValueError("extension state counters are invalid")
    if record.quarantine is not None and record.quarantine.extension_id != record.extension_id:
        raise ValueError("extension state quarantine identity mismatch")


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
            rows = connection.execute("SELECT slot, record_json FROM extension_registry_state ORDER BY slot").fetchall()
        decoded = tuple((slot, _decode(record_json)) for slot, record_json in rows)
        if any(slot != record.key[0] + ":" + record.key[1] + ":" + record.key[2] for slot, record in decoded):
            raise ValueError("extension state slot identity mismatch")
        records = tuple(record for _, record in decoded)
        if len(records) > self._max_records or len({record.key for record in records}) != len(records):
            raise ValueError("extension state is invalid or exceeds capacity")
        for record in records:
            _validate_record(record)
        return records

    def save(self, records: Sequence[ExtensionInstallRecord]) -> None:
        if len(records) > self._max_records or len({record.key for record in records}) != len(records):
            raise ValueError("extension state exceeds capacity")
        for record in records:
            _validate_record(record)
        encoded = [(record.key[0] + ":" + record.key[1] + ":" + record.key[2], json.dumps(_record(record), sort_keys=True, separators=(",", ":"))) for record in records]
        with sqlite3.connect(str(self._path)) as connection:
            connection.execute("DELETE FROM extension_registry_state")
            connection.executemany("INSERT INTO extension_registry_state(slot, record_json) VALUES (?, ?)", encoded)


__all__ = ["SQLiteExtensionStateRepository"]
