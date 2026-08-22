"""Provider-neutral application composition for production-bound updates.

This boundary performs no I/O itself. Artifact transfer, staging, health
checks, backup, platform activation, and durable journaling are injected
ports/adapters. A release cannot reach activation unless its metadata, signed
evidence, runtime contract, authority decision, health gate, and backup have
all passed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping

from ..ports.updates import UpdateAuthority, UpdateBackup, UpdatePort
from .bounded_state import BoundedUpdateState, UpdateSnapshot, UpdateTarget
from .durable_activation import DurableActivationCoordinator
from .release_evidence import ActivationRequest, ReleaseEvidencePackage


class UpdateAuthorizationError(PermissionError):
    """The caller did not provide positive authority for this activation."""


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    """Bounded state and verified artifact needed for activation."""

    state: BoundedUpdateState
    artifact: bytes
    backup_id: str

    @property
    def snapshot(self) -> UpdateSnapshot:
        return self.state.snapshot


class _DurableActivator:
    def __init__(self, coordinator: DurableActivationCoordinator,
                 activation_id: str, evidence: ReleaseEvidencePackage,
                 dependencies: Mapping[str, str]) -> None:
        self._coordinator = coordinator
        self._activation_id = activation_id
        self._evidence = evidence
        self._dependencies = dependencies

    def activate(self, request: ActivationRequest) -> str:
        return self._coordinator.activate(
            self._activation_id, request, self._evidence,
            observed_dependencies=self._dependencies,
        )


class UpdateApplicationService:
    """Compose one complete, fail-closed update lifecycle.

    Public methods are intended for the application thread. The service owns
    only in-flight bounded state; resource ownership stays with adapters.
    """

    def __init__(
        self, *, ports: UpdatePort, backup: UpdateBackup,
        activation: DurableActivationCoordinator,
        verifier: Callable[[bytes, str, str], bool],
        authority: UpdateAuthority,
    ) -> None:
        for name, value in (("ports", ports), ("backup", backup),
                            ("activation", activation), ("verifier", verifier),
                            ("authority", authority)):
            if value is None:
                raise TypeError(f"{name} is required")
        if not callable(getattr(authority, "authorize", None)):
            raise TypeError("authority must provide authorize")
        if (not callable(getattr(backup, "create", None))
                or not callable(getattr(backup, "restore", None))
                or not callable(getattr(backup, "verify", None))):
            raise TypeError("backup must provide create, verify, and restore")
        self._ports, self._backup = ports, backup
        self._activation, self._verifier = activation, verifier
        self._authority = authority

    def prepare(self, target: UpdateTarget, *, now: datetime | None = None) -> PreparedUpdate:
        """Download, authenticate, stage, health-check, then verify backup."""
        recording = _RecordingArtifactPort(self._ports)
        state = BoundedUpdateState(target)
        state.download(recording)
        artifact = recording.artifact
        if artifact is None:
            raise ValueError("download port did not return an artifact")
        state.verify(self._verifier, now=now)
        state.stage(self._ports, artifact)
        state.health_gate(self._ports)
        if not self._authority.authorize(target):
            raise UpdateAuthorizationError("update activation authority rejected release")
        backup_id = self._backup.create(target)
        if (not isinstance(backup_id, str) or not backup_id.strip()
                or not self._backup.verify(backup_id)):
            raise RuntimeError("update backup could not be verified")
        return PreparedUpdate(state, artifact, backup_id)

    def activate(
        self, prepared: PreparedUpdate, *, activation_id: str,
        request: ActivationRequest, observed_dependencies: Mapping[str, str],
    ) -> UpdateSnapshot:
        """Authorize and atomically activate a prepared update.

        The durable coordinator repeats signature, evidence, runtime-contract,
        and current-pointer checks immediately before activation. Backup
        restore is attempted on every activation exception.
        """
        target = prepared.snapshot.target
        if not self._authority.authorize(target):
            raise UpdateAuthorizationError("update activation authority rejected release")
        activator = _DurableActivator(
            self._activation, activation_id, target.evidence, observed_dependencies,
        )
        try:
            prepared.state.activate(activator, request)
        except Exception:
            try:
                self._backup.restore(prepared.backup_id)
            except Exception:
                # Preserve the original failure while making failed recovery
                # visible to the caller; the durable coordinator has already
                # recorded its independent activation recovery evidence.
                raise
            raise
        return prepared.snapshot


class _RecordingArtifactPort:
    def __init__(self, delegate: UpdatePort) -> None:
        self._delegate = delegate
        self.artifact: bytes | None = None

    def download(self, target: UpdateTarget) -> bytes:
        artifact = self._delegate.download(target)
        self.artifact = artifact
        return artifact

    def stage(self, target: UpdateTarget, artifact: bytes) -> str:
        return self._delegate.stage(target, artifact)

    def health_check(self, target: UpdateTarget, staged_ref: str) -> bool:
        return self._delegate.health_check(target, staged_ref)


__all__ = ["PreparedUpdate", "UpdateApplicationService", "UpdateAuthorizationError"]
