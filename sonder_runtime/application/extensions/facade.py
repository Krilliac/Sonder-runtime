"""Application-facing extension operations.

This facade is the narrow boundary used by interfaces.  It deliberately does
not install artifacts, persist registry state, promote experiments, or infer
authority from a caller's presence.  Every operation receives an explicit,
typed authority grant.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .experiments import EphemeralExperimentManager, ExperimentSnapshot
from .registry import (
    ExtensionInstallRecord,
    ExtensionRegistry,
    ExtensionRegistrySnapshot,
    ExtensionRepairDiagnostic,
)
from ...domain.extensions.manifest import ExtensionManifest
from ...domain.extensions.manifest import ExtensionDependency, ExtensionIdentity


def build_extension_manifest(
    extension_id: str,
    version: str,
    protocol: str,
    *,
    dependencies: Sequence[object] = (),
    permissions: Sequence[str] = (),
) -> ExtensionManifest:
    """Build a validated manifest for interface callers without domain imports."""
    if not all(isinstance(value, str) and value.strip() for value in (extension_id, version, protocol)):
        raise TypeError("extension identity, version, and protocol must be non-empty text")
    if extension_id.count(".") != 1:
        raise ValueError("extension identity must be publisher.name")
    publisher, name = extension_id.split(".")
    parsed_dependencies = []
    for item in dependencies:
        if isinstance(item, str):
            parsed_dependencies.append(ExtensionDependency(item, "*"))
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            parsed_dependencies.append(ExtensionDependency(item["name"], str(item.get("version", "*"))))
        else:
            raise TypeError("extension dependencies must be names or objects")
    if any(not isinstance(item, str) or not item for item in permissions):
        raise TypeError("extension permissions must be non-empty text")
    return ExtensionManifest(
        ExtensionIdentity(name, publisher), version, protocol,
        tuple(parsed_dependencies), tuple(permissions),
    )


class ExtensionFacadeError(RuntimeError):
    """Base error raised at the application-facing extension boundary."""


class ExtensionAuthorityDenied(ExtensionFacadeError, PermissionError):
    """The caller did not provide the explicit operation grant required."""


@dataclass(frozen=True, slots=True)
class ExtensionAuthority:
    """An explicit, narrow grant supplied by an authenticated interface."""

    actor: str
    operations: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ValueError("authority actor must be non-empty")
        if not isinstance(self.operations, frozenset) or not self.operations:
            raise ValueError("authority must grant at least one operation")

    def require(self, operation: str) -> None:
        if operation not in self.operations:
            raise ExtensionAuthorityDenied(
                f"actor {self.actor!r} is not authorized for extension operation {operation!r}"
            )


@dataclass(frozen=True, slots=True)
class ExtensionRegistryHealth:
    """Read-only registry projection with its non-persistence boundary stated."""

    snapshot: ExtensionRegistrySnapshot
    diagnostics: tuple[ExtensionRepairDiagnostic, ...]
    persistence: str = "in-memory-only"
    promotion: str = "not-supported"
    provenance_digest: str = ""
    provenance_records: int = 0


class ExtensionApplicationFacade:
    """Typed inspect/experiment/registry boundary for production interfaces."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        experiments: EphemeralExperimentManager,
    ) -> None:
        if not isinstance(registry, ExtensionRegistry):
            raise TypeError("registry must be an ExtensionRegistry")
        if not isinstance(experiments, EphemeralExperimentManager):
            raise TypeError("experiments must be an EphemeralExperimentManager")
        self._registry = registry
        self._experiments = experiments

    def registry_health(self, authority: ExtensionAuthority) -> ExtensionRegistryHealth:
        authority.require("registry_health")
        return ExtensionRegistryHealth(
            self._registry.snapshot(), self._registry.repair_diagnostics(),
            "durable" if self._registry.durable else "in-memory-only",
            "not-supported",
            self._registry.provenance_inventory.digest,
            len(self._registry.provenance_inventory.records),
        )

    def disable(self, extension_id: str, *, scope: str, project_id: str | None, authority: ExtensionAuthority):
        authority.require("disable")
        return self._registry.disable(extension_id, scope=scope, project_id=project_id)

    def enable(self, extension_id: str, *, scope: str, project_id: str | None, authority: ExtensionAuthority):
        authority.require("enable")
        return self._registry.enable(extension_id, scope=scope, project_id=project_id)

    def repair(self, extension_id: str, *, scope: str, project_id: str | None, authority: ExtensionAuthority):
        authority.require("repair")
        return self._registry.evaluate_health(extension_id, scope=scope, project_id=project_id)

    def update(self, manifest: ExtensionManifest, *, scope: str, project_id: str | None,
               authority: ExtensionAuthority):
        authority.require("update")
        return self._registry.update(manifest, scope=scope, project_id=project_id)

    def inspect(self, experiment_id: str, authority: ExtensionAuthority) -> ExperimentSnapshot:
        authority.require("inspect")
        return self._experiments.inspect(experiment_id)

    def define(
        self,
        experiment_id: str,
        argv: Sequence[str],
        *,
        authority: ExtensionAuthority,
        description: str = "",
        environment: Mapping[str, str] | None = None,
        limits: object | None = None,
    ) -> ExperimentSnapshot:
        authority.require("define")
        return self._experiments.define(
            experiment_id,
            argv,
            description=description,
            environment=environment,
            limits=limits,
        )

    def start(self, experiment_id: str, authority: ExtensionAuthority) -> ExperimentSnapshot:
        authority.require("start")
        return self._experiments.start(experiment_id)

    def stop(self, experiment_id: str, authority: ExtensionAuthority) -> ExperimentSnapshot:
        authority.require("stop")
        return self._experiments.stop(experiment_id)

    def delete(self, experiment_id: str, authority: ExtensionAuthority) -> ExperimentSnapshot:
        authority.require("delete")
        return self._experiments.delete(experiment_id)


__all__ = [
    "build_extension_manifest",
    "ExtensionApplicationFacade",
    "ExtensionAuthority",
    "ExtensionAuthorityDenied",
    "ExtensionFacadeError",
    "ExtensionRegistryHealth",
]
