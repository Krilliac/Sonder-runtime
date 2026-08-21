"""Deterministic, non-mutating verification evidence lifecycle for selfmod.

This module records evidence supplied by external verification, review, and
deployment adapters.  It intentionally does not run commands, touch files,
create backups, activate artifacts, inspect health, or perform rollback.
Those operations remain outside the application policy boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import re
from typing import Final


class LifecycleInputError(ValueError):
    """Raised when an evidence record is not auditable."""


class LifecycleConflict(RuntimeError):
    """Raised when a record cannot be added in the current lifecycle phase."""


class VerificationKind(str, Enum):
    TARGETED = "targeted"
    ARCHITECTURE = "architecture"
    REGRESSION = "regression"
    SMOKE = "smoke"


class LifecyclePhase(str, Enum):
    PROPOSED = "proposed"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    REVIEWED = "reviewed"
    BACKED_UP = "backed_up"
    ACTIVATED = "activated"
    HEALTH_FAILED = "health_failed"
    HEALTH_CHECKED = "health_checked"
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"
    FAILED = "failed"


class FailureState(str, Enum):
    NONE = "none"
    VERIFICATION_INCOMPLETE = "verification_incomplete"
    VERIFICATION_FAILED = "verification_failed"
    REVIEW_REQUIRED = "review_required"
    REVIEW_FAILED = "review_failed"
    BACKUP_FAILED = "backup_failed"
    ACTIVATION_FAILED = "activation_failed"
    HEALTH_FAILED = "health_failed"
    ROLLBACK_FAILED = "rollback_failed"


_SHA256: Final = re.compile(r"^[0-9a-fA-F]{64}$")
_REQUIRED_VERIFICATIONS: Final = frozenset(VerificationKind)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LifecycleInputError(f"{field} is required")
    return value.strip()


def _digest(value: object, field: str = "artifact_digest") -> str:
    value = _text(value, field)
    if not _SHA256.fullmatch(value):
        raise LifecycleInputError(f"{field} must be a SHA-256 hex digest")
    return value.lower()


@dataclass(frozen=True, slots=True)
class VerificationRecord:
    """One externally produced verification result."""

    evidence_id: str
    kind: VerificationKind
    passed: bool
    artifact_digest: str
    summary: str = ""

    def __post_init__(self) -> None:
        _text(self.evidence_id, "evidence_id")
        if not isinstance(self.kind, VerificationKind):
            raise LifecycleInputError("kind must be a VerificationKind")
        if not isinstance(self.passed, bool):
            raise LifecycleInputError("passed must be boolean")
        _digest(self.artifact_digest)
        if not isinstance(self.summary, str):
            raise LifecycleInputError("summary must be text")


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """Independent review, distinct from verification results."""

    review_id: str
    reviewer: str
    approved: bool
    evidence_ids: tuple[str, ...]
    summary: str = ""
    independent: bool = True

    def __post_init__(self) -> None:
        _text(self.review_id, "review_id")
        _text(self.reviewer, "reviewer")
        if not isinstance(self.approved, bool):
            raise LifecycleInputError("approved must be boolean")
        if not isinstance(self.independent, bool):
            raise LifecycleInputError("independent must be boolean")
        if not isinstance(self.evidence_ids, tuple) or not self.evidence_ids:
            raise LifecycleInputError("review must cite evidence ids")
        if any(not isinstance(item, str) or not item.strip() for item in self.evidence_ids):
            raise LifecycleInputError("review evidence ids must be non-empty text")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise LifecycleInputError("review evidence ids must be unique")
        if not isinstance(self.summary, str):
            raise LifecycleInputError("summary must be text")


@dataclass(frozen=True, slots=True)
class BackupRecord:
    evidence_id: str
    passed: bool
    artifact_digest: str
    summary: str = ""

    def __post_init__(self) -> None:
        _lifecycle_record(self.evidence_id, self.passed, self.artifact_digest, self.summary)


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    evidence_id: str
    passed: bool
    artifact_digest: str
    summary: str = ""

    def __post_init__(self) -> None:
        _lifecycle_record(self.evidence_id, self.passed, self.artifact_digest, self.summary)


@dataclass(frozen=True, slots=True)
class HealthRecord:
    evidence_id: str
    passed: bool
    artifact_digest: str
    summary: str = ""

    def __post_init__(self) -> None:
        _lifecycle_record(self.evidence_id, self.passed, self.artifact_digest, self.summary)


@dataclass(frozen=True, slots=True)
class RollbackRecord:
    evidence_id: str
    passed: bool
    artifact_digest: str
    summary: str = ""

    def __post_init__(self) -> None:
        _lifecycle_record(self.evidence_id, self.passed, self.artifact_digest, self.summary)


def _lifecycle_record(evidence_id: str, passed: bool, artifact_digest: str, summary: str) -> None:
    _text(evidence_id, "evidence_id")
    if not isinstance(passed, bool):
        raise LifecycleInputError("passed must be boolean")
    _digest(artifact_digest)
    if not isinstance(summary, str):
        raise LifecycleInputError("summary must be text")


@dataclass(frozen=True, slots=True)
class VerificationLifecycleRecord:
    candidate_id: str
    objective: str
    baseline_digest: str
    phase: LifecyclePhase = LifecyclePhase.PROPOSED
    failure: FailureState = FailureState.NONE
    verifications: tuple[VerificationRecord, ...] = ()
    review: ReviewRecord | None = None
    backup: BackupRecord | None = None
    activation: ActivationRecord | None = None
    health: HealthRecord | None = None
    rollback: RollbackRecord | None = None

    def __post_init__(self) -> None:
        _text(self.candidate_id, "candidate_id")
        _text(self.objective, "objective")
        _digest(self.baseline_digest, "baseline_digest")
        if not isinstance(self.phase, LifecyclePhase):
            raise LifecycleInputError("phase must be a LifecyclePhase")
        if not isinstance(self.failure, FailureState):
            raise LifecycleInputError("failure must be a FailureState")
        if not isinstance(self.verifications, tuple):
            raise LifecycleInputError("verifications must be a tuple")
        if len({item.kind for item in self.verifications}) != len(self.verifications):
            raise LifecycleInputError("verification kinds must be unique")


class VerificationLifecycle:
    """In-memory lifecycle coordinator with explicit, typed failure states."""

    def __init__(self) -> None:
        self._records: dict[str, VerificationLifecycleRecord] = {}

    def propose(self, candidate_id: str, objective: str, baseline_digest: str) -> VerificationLifecycleRecord:
        candidate_id = _text(candidate_id, "candidate_id")
        if candidate_id in self._records:
            raise LifecycleConflict(f"candidate {candidate_id!r} already exists")
        record = VerificationLifecycleRecord(candidate_id, _text(objective, "objective"), _digest(baseline_digest, "baseline_digest"))
        self._records[candidate_id] = record
        return record

    def record_verification(self, candidate_id: str, evidence: VerificationRecord) -> VerificationLifecycleRecord:
        record = self._get(candidate_id)
        self._require(record, LifecyclePhase.PROPOSED, LifecyclePhase.VERIFYING)
        if any(item.evidence_id == evidence.evidence_id for item in record.verifications):
            raise LifecycleConflict(f"verification {evidence.evidence_id!r} already exists")
        if any(item.kind is evidence.kind for item in record.verifications):
            raise LifecycleConflict(f"verification kind {evidence.kind.value!r} already exists")
        verifications = record.verifications + (evidence,)
        if not evidence.passed:
            return self._fail(
                record,
                failure=FailureState.VERIFICATION_FAILED,
                verifications=verifications,
            )
        phase = LifecyclePhase.VERIFIED if {item.kind for item in verifications} == _REQUIRED_VERIFICATIONS else LifecyclePhase.VERIFYING
        updated = replace(record, phase=phase, failure=FailureState.NONE, verifications=verifications)
        return self._store(updated)

    def record_review(self, candidate_id: str, review: ReviewRecord) -> VerificationLifecycleRecord:
        record = self._get(candidate_id)
        self._require(record, LifecyclePhase.VERIFIED)
        known = {item.evidence_id for item in record.verifications}
        if set(review.evidence_ids) != known:
            raise LifecycleInputError("independent review must cite every verification record exactly")
        if not review.independent:
            return self._fail(record, review=review, failure=FailureState.REVIEW_REQUIRED)
        if not review.approved:
            return self._fail(record, review=review, failure=FailureState.REVIEW_FAILED)
        return self._store(replace(record, phase=LifecyclePhase.REVIEWED, failure=FailureState.NONE, review=review))

    def record_backup(self, candidate_id: str, evidence: BackupRecord) -> VerificationLifecycleRecord:
        record = self._get(candidate_id)
        self._require(record, LifecyclePhase.REVIEWED)
        if not evidence.passed:
            return self._fail(record, backup=evidence, failure=FailureState.BACKUP_FAILED)
        return self._store(replace(record, phase=LifecyclePhase.BACKED_UP, failure=FailureState.NONE, backup=evidence))

    def record_activation(self, candidate_id: str, evidence: ActivationRecord) -> VerificationLifecycleRecord:
        record = self._get(candidate_id)
        self._require(record, LifecyclePhase.BACKED_UP)
        if not evidence.passed:
            return self._fail(record, activation=evidence, failure=FailureState.ACTIVATION_FAILED)
        return self._store(replace(record, phase=LifecyclePhase.ACTIVATED, failure=FailureState.NONE, activation=evidence))

    def record_health(self, candidate_id: str, evidence: HealthRecord) -> VerificationLifecycleRecord:
        record = self._get(candidate_id)
        self._require(record, LifecyclePhase.ACTIVATED)
        if not evidence.passed:
            return self._store(replace(record, phase=LifecyclePhase.HEALTH_FAILED, failure=FailureState.HEALTH_FAILED, health=evidence))
        return self._store(replace(record, phase=LifecyclePhase.COMPLETED, failure=FailureState.NONE, health=evidence))

    def record_rollback(self, candidate_id: str, evidence: RollbackRecord) -> VerificationLifecycleRecord:
        record = self._get(candidate_id)
        self._require(record, LifecyclePhase.HEALTH_FAILED)
        if not evidence.passed:
            return self._fail(record, rollback=evidence, failure=FailureState.ROLLBACK_FAILED)
        return self._store(replace(record, phase=LifecyclePhase.ROLLED_BACK, failure=FailureState.NONE, rollback=evidence))

    def get(self, candidate_id: str) -> VerificationLifecycleRecord:
        return self._get(candidate_id)

    def snapshot(self) -> tuple[VerificationLifecycleRecord, ...]:
        """Return records in deterministic candidate-id order."""
        return tuple(self._records[key] for key in sorted(self._records))

    def _get(self, candidate_id: str) -> VerificationLifecycleRecord:
        record = self._records.get(candidate_id)
        if record is None:
            raise LifecycleInputError(f"candidate {candidate_id!r} not found")
        return record

    @staticmethod
    def _require(record: VerificationLifecycleRecord, *phases: LifecyclePhase) -> None:
        if record.phase not in phases:
            raise LifecycleConflict(f"candidate is {record.phase.value}; expected one of {', '.join(item.value for item in phases)}")

    def _store(self, record: VerificationLifecycleRecord) -> VerificationLifecycleRecord:
        self._records[record.candidate_id] = record
        return record

    def _fail(self, record: VerificationLifecycleRecord, *, failure: FailureState, **fields: object) -> VerificationLifecycleRecord:
        return self._store(replace(record, phase=LifecyclePhase.FAILED, failure=failure, **fields))


__all__ = [
    "ActivationRecord", "BackupRecord", "FailureState", "HealthRecord",
    "LifecycleConflict", "LifecycleInputError", "LifecyclePhase", "ReviewRecord",
    "RollbackRecord", "VerificationKind", "VerificationLifecycle",
    "VerificationLifecycleRecord", "VerificationRecord",
]
