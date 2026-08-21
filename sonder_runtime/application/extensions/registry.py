"""Bounded, in-memory extension installation and health registry.

This module owns only typed state.  It does not discover artifacts, read or
write files, contact a registry, or start extension processes.  Admission and
quarantine decisions are delegated to the existing extension contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
from typing import Iterable, Mapping

from sonder_runtime.application.extensions.provenance_inventory import (
    ExtensionHealth as ProvenanceHealth,
    ExtensionHealthState,
    ExtensionProvenanceAdmission,
    ProvenanceInventory,
)
from sonder_runtime.application.extensions.quarantine import (
    QuarantineDecision,
    QuarantineRegistry,
)
from sonder_runtime.domain.extensions.manifest import ExtensionManifest


class ExtensionRegistryError(ValueError):
    """Base error for invalid registry operations."""


class ExtensionNotFoundError(ExtensionRegistryError):
    """Raised when an operation addresses an unknown installation."""


class ExtensionAlreadyInstalledError(ExtensionRegistryError):
    """Raised when an installation key already exists without replacement."""


class ExtensionScope(StrEnum):
    PROJECT = "project"
    GLOBAL = "global"


@dataclass(frozen=True, slots=True)
class ExtensionInstallRecord:
    """The complete state of one installation slot."""

    extension_id: str
    scope: ExtensionScope
    project_id: str | None
    version: str
    manifest_digest: str
    manifest: ExtensionManifest
    enabled: bool
    health_state: ExtensionHealthState
    health_reasons: tuple[str, ...] = ()
    quarantine: QuarantineDecision | None = None
    crash_count: int = 0

    @property
    def healthy(self) -> bool:
        return self.health_state is ExtensionHealthState.HEALTHY

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.scope.value, self.project_id or "", self.extension_id)


@dataclass(frozen=True, slots=True)
class ExtensionRepairDiagnostic:
    extension_id: str
    scope: ExtensionScope
    project_id: str | None
    state: ExtensionHealthState
    codes: tuple[str, ...]
    recommended_action: str


@dataclass(frozen=True, slots=True)
class ExtensionRegistrySnapshot:
    records: tuple[ExtensionInstallRecord, ...]
    digest: str

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


def _scope(value: ExtensionScope | str) -> ExtensionScope:
    try:
        return value if isinstance(value, ExtensionScope) else ExtensionScope(value)
    except (TypeError, ValueError) as error:
        raise ExtensionRegistryError("scope must be 'project' or 'global'") from error


def _project_id(scope: ExtensionScope, project_id: str | None) -> str | None:
    if scope is ExtensionScope.PROJECT:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ExtensionRegistryError("project scope requires a non-empty project_id")
        return project_id.strip()
    if project_id is not None:
        raise ExtensionRegistryError("global scope cannot have a project_id")
    return None


def _canonical_record(record: ExtensionInstallRecord) -> dict[str, object]:
    return {
        "extension_id": record.extension_id,
        "scope": record.scope.value,
        "project_id": record.project_id,
        "version": record.version,
        "manifest_digest": record.manifest_digest,
        "enabled": record.enabled,
        "health_state": record.health_state.value,
        "health_reasons": list(record.health_reasons),
        "quarantine": None if record.quarantine is None else {
            "quarantined": record.quarantine.quarantined,
            "reasons": list(record.quarantine.reasons),
            "cleanup_action": record.quarantine.cleanup_action,
            "retain_state": record.quarantine.retain_state,
        },
        "crash_count": record.crash_count,
    }


class ExtensionRegistry:
    """A bounded registry for project and global extension installations."""

    def __init__(
        self,
        *,
        quarantine: QuarantineRegistry | None = None,
        provenance: ProvenanceInventory | None = None,
        admission: ExtensionProvenanceAdmission | None = None,
        repository: object | None = None,
        max_records: int = 256,
    ) -> None:
        if max_records < 1 or max_records > 4096:
            raise ExtensionRegistryError("max_records must be between 1 and 4096")
        if admission is not None and provenance is not None:
            raise ExtensionRegistryError("provide admission or provenance, not both")
        self._quarantine = quarantine or QuarantineRegistry()
        self._admission = admission or (
            ExtensionProvenanceAdmission(provenance, self._quarantine) if provenance is not None else None
        )
        self._max_records = max_records
        self._repository = repository
        self._records: dict[tuple[str, str, str], ExtensionInstallRecord] = {}
        if repository is not None:
            loader = getattr(repository, "load", None)
            if not callable(loader):
                raise TypeError("repository must provide load and save")
            loaded = tuple(loader())
            if len(loaded) > max_records:
                raise ExtensionRegistryError("persisted extension state exceeds capacity")
            for record in loaded:
                if not isinstance(record, ExtensionInstallRecord):
                    raise ExtensionRegistryError("persisted extension state is invalid")
                self._records[record.key] = record
                self._quarantine.restore_crash_count(record.extension_id, record.crash_count)

    def install(
        self,
        manifest: ExtensionManifest,
        *,
        scope: ExtensionScope | str,
        project_id: str | None = None,
        protocol: str | None = None,
        available_dependencies: Iterable[str] = (),
        granted_permissions: Iterable[str] = (),
        signatures_verified: bool = False,
        replace: bool = False,
    ) -> ExtensionInstallRecord:
        normalized_scope = _scope(scope)
        normalized_project = _project_id(normalized_scope, project_id)
        key = (normalized_scope.value, normalized_project or "", manifest.extension_id)
        if key in self._records and not replace:
            raise ExtensionAlreadyInstalledError(f"extension already installed: {manifest.extension_id}")
        if key not in self._records and len(self._records) >= self._max_records:
            raise ExtensionRegistryError("extension registry capacity exhausted")
        health, quarantine = self._evaluate(
            manifest,
            protocol=protocol or manifest.protocol,
            available_dependencies=set(available_dependencies),
            granted_permissions=set(granted_permissions),
            signatures_verified=signatures_verified,
        )
        record = self._make_record(
            manifest, normalized_scope, normalized_project, health, quarantine,
            enabled=health.state is ExtensionHealthState.HEALTHY,
            crash_count=self._records[key].crash_count if key in self._records else 0,
        )
        self._records[key] = record
        self._persist()
        return record

    def update(self, manifest: ExtensionManifest, **kwargs: object) -> ExtensionInstallRecord:
        """Replace an existing installation record after re-evaluating it."""
        kwargs["replace"] = True
        return self.install(manifest, **kwargs)  # type: ignore[arg-type]

    replace = update

    @property
    def durable(self) -> bool:
        return self._repository is not None

    def evaluate_health(
        self,
        extension_id: str,
        *,
        scope: ExtensionScope | str,
        project_id: str | None = None,
        protocol: str | None = None,
        available_dependencies: Iterable[str] = (),
        granted_permissions: Iterable[str] = (),
        signatures_verified: bool = False,
    ) -> ExtensionInstallRecord:
        record = self._get(extension_id, scope, project_id)
        health, quarantine = self._evaluate(
            record.manifest,
            protocol=protocol or record.manifest.protocol,
            available_dependencies=set(available_dependencies),
            granted_permissions=set(granted_permissions),
            signatures_verified=signatures_verified,
        )
        updated = self._make_record(
            record.manifest, record.scope, record.project_id, health, quarantine,
            enabled=record.enabled and health.state is ExtensionHealthState.HEALTHY,
            crash_count=record.crash_count,
        )
        self._records[record.key] = updated
        self._persist()
        return updated

    def record_crash(
        self, extension_id: str, *, scope: ExtensionScope | str, project_id: str | None = None
    ) -> ExtensionInstallRecord:
        record = self._get(extension_id, scope, project_id)
        decision = self._quarantine.record_crash(record.manifest)
        health_state = ExtensionHealthState.QUARANTINED if decision.quarantined else record.health_state
        reasons = decision.reasons if decision.quarantined else record.health_reasons
        updated = ExtensionInstallRecord(
            record.extension_id, record.scope, record.project_id, record.version,
            record.manifest_digest, record.manifest, record.enabled and not decision.quarantined,
            health_state, tuple(reasons), decision if decision.quarantined else record.quarantine,
            self._quarantine.crash_count(record.extension_id),
        )
        self._records[record.key] = updated
        self._persist()
        return updated

    def disable(self, extension_id: str, *, scope: ExtensionScope | str, project_id: str | None = None) -> ExtensionInstallRecord:
        return self._set_enabled(extension_id, scope=scope, project_id=project_id, enabled=False)

    def enable(self, extension_id: str, *, scope: ExtensionScope | str, project_id: str | None = None) -> ExtensionInstallRecord:
        record = self._get(extension_id, scope, project_id)
        if not record.healthy:
            return record
        return self._set_enabled(extension_id, scope=scope, project_id=project_id, enabled=True)

    def remove(self, extension_id: str, *, scope: ExtensionScope | str, project_id: str | None = None) -> None:
        del self._records[self._key(extension_id, scope, project_id)]
        self._persist()

    def get(self, extension_id: str, *, scope: ExtensionScope | str, project_id: str | None = None) -> ExtensionInstallRecord:
        return self._get(extension_id, scope, project_id)

    def snapshot(self) -> ExtensionRegistrySnapshot:
        records = tuple(self._records[key] for key in sorted(self._records))
        material = [_canonical_record(record) for record in records]
        digest = sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return ExtensionRegistrySnapshot(records, digest)

    def repair_diagnostics(self) -> tuple[ExtensionRepairDiagnostic, ...]:
        diagnostics: list[ExtensionRepairDiagnostic] = []
        for record in self.snapshot().records:
            codes = list(record.health_reasons)
            if not record.enabled and record.healthy:
                codes.append("disabled")
            if record.health_state is ExtensionHealthState.UNVERIFIED and not codes:
                codes.append("provenance-unverified")
            if record.health_state is ExtensionHealthState.QUARANTINED and not codes:
                codes.append("quarantined")
            if not codes:
                continue
            action = "review-provenance" if record.health_state is ExtensionHealthState.UNVERIFIED else (
                "repair-compatibility" if record.health_state is ExtensionHealthState.INCOMPATIBLE else "inspect-quarantine"
            )
            diagnostics.append(ExtensionRepairDiagnostic(
                record.extension_id, record.scope, record.project_id, record.health_state,
                tuple(dict.fromkeys(codes)), action,
            ))
        return tuple(diagnostics)

    def _evaluate(self, manifest: ExtensionManifest, *, protocol: str, available_dependencies: set[str], granted_permissions: set[str], signatures_verified: bool) -> tuple[ProvenanceHealth, QuarantineDecision | None]:
        if self._admission is not None:
            try:
                health = self._admission.evaluate(
                    manifest, protocol=protocol, available_dependencies=available_dependencies,
                    granted_permissions=granted_permissions, signatures_verified=signatures_verified,
                )
            except KeyError:
                health = ProvenanceHealth(manifest.extension_id, ExtensionHealthState.UNVERIFIED, ("provenance-missing",))
            return health, health.quarantine
        decision = self._quarantine.evaluate(
            manifest, protocol=protocol, available_dependencies=available_dependencies,
            granted_permissions=granted_permissions,
        )
        state = ExtensionHealthState.QUARANTINED if decision.quarantined else ExtensionHealthState.HEALTHY
        health = ProvenanceHealth(manifest.extension_id, state, decision.reasons, decision)
        return health, decision

    @staticmethod
    def _make_record(manifest: ExtensionManifest, scope: ExtensionScope, project_id: str | None, health: ProvenanceHealth, quarantine: QuarantineDecision | None, *, enabled: bool, crash_count: int) -> ExtensionInstallRecord:
        return ExtensionInstallRecord(
            manifest.extension_id, scope, project_id, manifest.version, manifest.digest(), manifest,
            enabled, health.state, tuple(health.reasons), quarantine, crash_count,
        )

    def _set_enabled(self, extension_id: str, *, scope: ExtensionScope | str, project_id: str | None, enabled: bool) -> ExtensionInstallRecord:
        record = self._get(extension_id, scope, project_id)
        updated = ExtensionInstallRecord(
            record.extension_id, record.scope, record.project_id, record.version, record.manifest_digest,
            record.manifest, enabled, record.health_state, record.health_reasons,
            record.quarantine, record.crash_count,
        )
        self._records[record.key] = updated
        return updated

    def _get(self, extension_id: str, scope: ExtensionScope | str, project_id: str | None) -> ExtensionInstallRecord:
        key = self._key(extension_id, scope, project_id)
        try:
            return self._records[key]
        except KeyError as error:
            raise ExtensionNotFoundError(f"extension is not installed: {extension_id}") from error

    def _persist(self) -> None:
        if self._repository is not None:
            saver = getattr(self._repository, "save", None)
            if not callable(saver):
                raise TypeError("repository must provide load and save")
            saver(self.snapshot().records)

    @staticmethod
    def _key(extension_id: str, scope: ExtensionScope | str, project_id: str | None) -> tuple[str, str, str]:
        normalized_scope = _scope(scope)
        normalized_project = _project_id(normalized_scope, project_id)
        return (normalized_scope.value, normalized_project or "", extension_id)


__all__ = [
    "ExtensionAlreadyInstalledError", "ExtensionInstallRecord", "ExtensionNotFoundError",
    "ExtensionRegistry", "ExtensionRegistryError", "ExtensionRegistrySnapshot", "ExtensionRepairDiagnostic",
    "ExtensionScope",
]
