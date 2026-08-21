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
            self._registry.snapshot(), self._registry.repair_diagnostics()
        )

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
    "ExtensionApplicationFacade",
    "ExtensionAuthority",
    "ExtensionAuthorityDenied",
    "ExtensionFacadeError",
    "ExtensionRegistryHealth",
]
