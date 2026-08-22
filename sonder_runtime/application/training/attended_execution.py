"""Attended adaptive-training execution boundary.

This service sequences side effects but performs none itself.  Every external
operation is a typed port, and missing/invalid evidence stops before launch.
"""
from __future__ import annotations

from dataclasses import dataclass

from ...domain.common.errors import Conflict, Forbidden, InvalidInput
from ...domain.training.reproducible import ReproducibleTrainingManifest
from ..ports.training import (
    JournalEvent,
    ManifestEvidence,
    ManifestVerifierPort,
    OllamaPolicyPort,
    TrainingDeploymentPort,
    TrainingJournalPort,
    TrainingLaunchRequest,
    TrainingLockPort,
    TrainingProcessPort,
)


@dataclass(frozen=True, slots=True)
class AttendedTrainingRequest:
    run_id: str
    command: tuple[str, ...]
    manifest: ReproducibleTrainingManifest
    signature: str
    attended: bool = False


@dataclass(frozen=True, slots=True)
class AttendedTrainingResult:
    run_id: str
    manifest_digest: str
    adapter_digest: str
    deployment: object


class AttendedTrainingExecutionService:
    """Coordinate launch, evidence, policy mutation, and recovery."""

    def __init__(
        self,
        *,
        process: TrainingProcessPort,
        lock: TrainingLockPort,
        verifier: ManifestVerifierPort,
        journal: TrainingJournalPort,
        policy: OllamaPolicyPort,
        deployment: TrainingDeploymentPort,
    ) -> None:
        self._process = process
        self._lock = lock
        self._verifier = verifier
        self._journal = journal
        self._policy = policy
        self._deployment = deployment

    def execute(self, request: AttendedTrainingRequest) -> AttendedTrainingResult:
        if not request.attended:
            raise Forbidden("adaptive training requires an attended operator")
        if not request.run_id.strip() or not request.command:
            raise InvalidInput("run_id and a non-empty training command are required")
        digest = request.manifest.digest
        if not digest or not self._verifier.verify(
            ManifestEvidence(digest, request.signature)
        ):
            raise Forbidden("signed immutable training manifest evidence was rejected")

        with self._lock.acquire(request.run_id):
            self._journal.append(JournalEvent(request.run_id, "authorized", digest))
            launch = self._process.launch(
                TrainingLaunchRequest(request.run_id, request.command, digest)
            )
            if launch.exit_code != 0 or not launch.adapter_digest:
                self._journal.append(
                    JournalEvent(request.run_id, "failed", digest, launch.detail)
                )
                raise Conflict("training process failed; deployment was not attempted")

            reservation = self._policy.reserve(request.run_id, launch.adapter_digest)
            try:
                self._journal.append(
                    JournalEvent(request.run_id, "candidate", digest, launch.adapter_digest)
                )
                deployment = self._deployment.activate(
                    launch.adapter_digest, attended=True
                )
                self._policy.commit(reservation)
                self._journal.append(JournalEvent(request.run_id, "deployed", digest))
                return AttendedTrainingResult(
                    request.run_id, digest, launch.adapter_digest, deployment
                )
            except Exception as exc:
                try:
                    self._policy.restore(reservation)
                    self._deployment.rollback(
                        attended=True, reason="training activation failed"
                    )
                    self._journal.append(
                        JournalEvent(request.run_id, "rolled_back", digest, str(exc)[:160])
                    )
                except Exception as recovery_error:
                    self._journal.append(
                        JournalEvent(
                            request.run_id,
                            "recovery_required",
                            digest,
                            str(recovery_error)[:160],
                        )
                    )
                raise


__all__ = [
    "AttendedTrainingExecutionService",
    "AttendedTrainingRequest",
    "AttendedTrainingResult",
]
