"""Durable, typed activation coordination for UPDATE-002/004."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from .release_evidence import (
    ActivationRecoveryError, ActivationRequest, PlatformActivationHelper,
    RecoveryEvidenceSink, ReleaseEvidencePackage, ReleasePointer,
    StandaloneRecoveryEvidence,
)


@dataclass(frozen=True, slots=True)
class ActivationJournalEntry:
    activation_id: str
    phase: str
    platform: str
    current_release: str
    target_release: str
    evidence_digest: str
    recovery_digest: str = ""
    error_types: tuple[str, ...] = ()


class ActivationJournal(Protocol):
    def append(self, entry: ActivationJournalEntry) -> None: ...
    def entries(self) -> tuple[ActivationJournalEntry, ...]: ...


class DurableActivationCoordinator:
    """Sequence durable evidence, helper activation, and atomic recovery."""

    def __init__(self, pointer: ReleasePointer, helper: PlatformActivationHelper,
                 journal: ActivationJournal,
                 verifier: Callable[[bytes, str, str], bool],
                 recovery_sink: RecoveryEvidenceSink | None = None) -> None:
        if not callable(getattr(helper, "activate", None)) or not callable(getattr(helper, "rollback", None)):
            raise TypeError("platform activation helper must provide activate and rollback")
        if not callable(getattr(pointer, "current", None)) or not callable(getattr(pointer, "commit", None)):
            raise TypeError("release pointer must provide current and commit")
        self._pointer, self._helper, self._journal = pointer, helper, journal
        self._verifier, self._recovery_sink = verifier, recovery_sink

    def activate(self, activation_id: str, request: ActivationRequest,
                 evidence: ReleaseEvidencePackage, *,
                 observed_dependencies: Mapping[str, str]) -> str:
        if not isinstance(activation_id, str) or not activation_id.strip():
            raise ValueError("activation_id must be non-empty")
        if request.release_evidence_digest != evidence.package_digest:
            raise ValueError("activation evidence digest does not match request")
        if request.target_release != evidence.manifest.release_id:
            raise ValueError("activation target does not match release evidence")
        evidence.verify(self._verifier, expected_runtime_contract=observed_dependencies)
        current = self._pointer.current()
        if current != request.current_release:
            raise RuntimeError("current release changed before activation")
        self._journal.append(self._entry(activation_id, "prepared", request))
        try:
            self._helper.activate(request)
            self._pointer.commit(request.target_release)
        except Exception as activation_error:
            recovery = self._recover(request)
            self._journal.append(self._entry(
                activation_id, "recovered" if recovery.pointer_restored else "recovery_failed",
                request, recovery_digest=recovery.digest, error_types=recovery.error_types,
            ))
            if self._recovery_sink is not None:
                self._recovery_sink.record(recovery)
            if not recovery.pointer_restored:
                raise ActivationRecoveryError(recovery) from activation_error
            raise
        self._journal.append(self._entry(activation_id, "activated", request))
        return request.target_release

    def _recover(self, request: ActivationRequest) -> StandaloneRecoveryEvidence:
        errors: list[str] = []
        rollback_request = ActivationRequest(
            request.platform, request.target_release, request.current_release,
            request.release_evidence_digest, request.helper_nonce, request.helper_argv,
        )
        try:
            self._helper.rollback(rollback_request)
        except Exception as error:
            errors.append(type(error).__name__)
        restored = False
        try:
            self._pointer.commit(request.current_release)
            restored = self._pointer.current() == request.current_release
        except Exception as error:
            errors.append(type(error).__name__)
        return StandaloneRecoveryEvidence(
            request.platform, request.current_release, request.target_release,
            True, True, restored, tuple(errors),
        )

    @staticmethod
    def _entry(activation_id: str, phase: str, request: ActivationRequest,
               *, recovery_digest: str = "", error_types: tuple[str, ...] = ()) -> ActivationJournalEntry:
        return ActivationJournalEntry(
            activation_id, phase, request.platform, request.current_release,
            request.target_release, request.release_evidence_digest,
            recovery_digest, error_types,
        )


__all__ = ["ActivationJournal", "ActivationJournalEntry", "DurableActivationCoordinator"]
