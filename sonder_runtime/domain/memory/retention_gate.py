"""Pure, provider-neutral memory-retention eligibility decisions.

This module describes the evidence required before a storage adapter may
physically remove one versioned memory record.  It does not open a database,
delete a file, replicate data, contact a provider, or implement consensus.
Callers supply the current record identity and bounded evidence; the returned
decision is an explainable, deterministic gate that an adapter must re-check
at its own side-effect boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


_MAX_TEXT = 256
_MAX_EVIDENCE = 256
_MAX_REASON_CODES = 12
_MAX_EXPLANATION_CHARS = 512


def _bounded_text(value: str, label: str, *, maximum: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty")
    if len(value) > maximum:
        raise ValueError(f"{label} must be at most {maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} must not contain control characters")
    return value


def _positive_int(value: int, label: str, *, zero_allowed: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < (0 if zero_allowed else 1):
        qualifier = "non-negative" if zero_allowed else "positive"
        raise ValueError(f"{label} must be {qualifier}")


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


class RetentionDecisionStatus(StrEnum):
    """Whether the supplied evidence permits an adapter to delete."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"


class MemoryRetentionReason(StrEnum):
    """Stable, bounded reason codes for a blocked retention decision."""

    RETENTION_NOT_EXPIRED = "retention_not_expired"
    JOB_REFERENCE_IDENTITY_MISMATCH = "job_reference_identity_mismatch"
    ACTIVE_JOB_REFERENCE = "active_job_reference"
    DEPLOYMENT_REFERENCE_IDENTITY_MISMATCH = "deployment_reference_identity_mismatch"
    ACTIVE_DEPLOYMENT_REFERENCE = "active_deployment_reference"
    BACKUP_ACKNOWLEDGEMENT_MISMATCH = "backup_acknowledgement_mismatch"
    BACKUP_ACKNOWLEDGEMENT_MISSING = "backup_acknowledgement_missing"
    REPLICA_ACKNOWLEDGEMENT_MISMATCH = "replica_acknowledgement_mismatch"
    REPLICA_ACKNOWLEDGEMENT_MISSING = "replica_acknowledgement_missing"


class MemoryAcknowledgementKind(StrEnum):
    """The independent durable holder that supplied an acknowledgement."""

    BACKUP = "backup"
    REPLICA = "replica"


@dataclass(frozen=True, slots=True)
class MemoryRecordIdentity:
    """Exact project-scoped identity for the record being considered."""

    project_scope: str
    record_id: str
    record_version: int
    tombstone_id: str

    def __post_init__(self) -> None:
        _bounded_text(self.project_scope, "project_scope")
        _bounded_text(self.record_id, "record_id")
        _positive_int(self.record_version, "record_version")
        _bounded_text(self.tombstone_id, "tombstone_id")

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_scope": self.project_scope,
            "record_id": self.record_id,
            "record_version": self.record_version,
            "tombstone_id": self.tombstone_id,
        }


@dataclass(frozen=True, slots=True)
class MemoryRetentionPolicy:
    """Explicit retention and durability requirements for one deletion gate.

    ``retain_until=None`` means that no expiry was supplied and therefore the
    record is retained indefinitely.  A single-PC deployment may explicitly
    set ``required_replica_acknowledgements=0`` when its backup policy is the
    only durable copy; the default requires one independently named replica.
    """

    retain_until: datetime | None = None
    require_backup_acknowledgement: bool = True
    required_replica_acknowledgements: int = 1

    def __post_init__(self) -> None:
        if self.retain_until is not None:
            _aware_utc(self.retain_until, "retain_until")
        if not isinstance(self.require_backup_acknowledgement, bool):
            raise ValueError("require_backup_acknowledgement must be boolean")
        _positive_int(
            self.required_replica_acknowledgements,
            "required_replica_acknowledgements",
            zero_allowed=True,
        )

    def expired(self, *, now: datetime) -> bool:
        """Return whether the explicit retention window has expired."""

        current = _aware_utc(now, "now")
        return self.retain_until is not None and current >= _aware_utc(
            self.retain_until, "retain_until"
        )


@dataclass(frozen=True, slots=True)
class ActiveMemoryReference:
    """A job or deployment reference that can keep a record live."""

    reference_id: str
    project_scope: str
    record_id: str
    record_version: int
    tombstone_id: str

    def __post_init__(self) -> None:
        _bounded_text(self.reference_id, "reference_id")
        _bounded_text(self.project_scope, "project_scope")
        _bounded_text(self.record_id, "record_id")
        _positive_int(self.record_version, "record_version")
        _bounded_text(self.tombstone_id, "tombstone_id")

    def matches(self, record: MemoryRecordIdentity) -> bool:
        return (
            self.project_scope == record.project_scope
            and self.record_id == record.record_id
            and self.record_version == record.record_version
            and self.tombstone_id == record.tombstone_id
        )


@dataclass(frozen=True, slots=True)
class MemoryAcknowledgement:
    """Version/tombstone-bound acknowledgement from backup or replica state."""

    acknowledgement_id: str
    holder_id: str
    kind: MemoryAcknowledgementKind
    project_scope: str
    record_id: str
    record_version: int
    tombstone_id: str

    def __post_init__(self) -> None:
        _bounded_text(self.acknowledgement_id, "acknowledgement_id")
        _bounded_text(self.holder_id, "holder_id")
        if not isinstance(self.kind, MemoryAcknowledgementKind):
            raise ValueError("kind must be a MemoryAcknowledgementKind")
        _bounded_text(self.project_scope, "project_scope")
        _bounded_text(self.record_id, "record_id")
        _positive_int(self.record_version, "record_version")
        _bounded_text(self.tombstone_id, "tombstone_id")

    def matches(self, record: MemoryRecordIdentity) -> bool:
        return (
            self.project_scope == record.project_scope
            and self.record_id == record.record_id
            and self.record_version == record.record_version
            and self.tombstone_id == record.tombstone_id
        )


def _evidence_tuple(values: object, label: str) -> tuple[object, ...]:
    if not isinstance(values, tuple):
        raise ValueError(f"{label} must be an immutable tuple")
    if len(values) > _MAX_EVIDENCE:
        raise ValueError(f"{label} must contain at most {_MAX_EVIDENCE} entries")
    return values


@dataclass(frozen=True, slots=True)
class MemoryRetentionRequest:
    """Bounded evidence presented to :func:`decide_memory_retention`."""

    record: MemoryRecordIdentity
    policy: MemoryRetentionPolicy
    active_job_references: tuple[ActiveMemoryReference, ...] = ()
    active_deployment_references: tuple[ActiveMemoryReference, ...] = ()
    backup_acknowledgements: tuple[MemoryAcknowledgement, ...] = ()
    replica_acknowledgements: tuple[MemoryAcknowledgement, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.record, MemoryRecordIdentity):
            raise ValueError("record must be a MemoryRecordIdentity")
        if not isinstance(self.policy, MemoryRetentionPolicy):
            raise ValueError("policy must be a MemoryRetentionPolicy")
        all_values = (
            (self.active_job_references, "active_job_references", ActiveMemoryReference),
            (
                self.active_deployment_references,
                "active_deployment_references",
                ActiveMemoryReference,
            ),
            (self.backup_acknowledgements, "backup_acknowledgements", MemoryAcknowledgement),
            (
                self.replica_acknowledgements,
                "replica_acknowledgements",
                MemoryAcknowledgement,
            ),
        )
        for values, label, expected_type in all_values:
            items = _evidence_tuple(values, label)
            if any(not isinstance(item, expected_type) for item in items):
                raise ValueError(f"{label} contains an unexpected evidence type")

        reference_ids = [
            item.reference_id
            for values in (self.active_job_references, self.active_deployment_references)
            for item in values
        ]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("active reference IDs must be unique")
        acknowledgement_ids = [
            item.acknowledgement_id
            for values in (self.backup_acknowledgements, self.replica_acknowledgements)
            for item in values
        ]
        if len(acknowledgement_ids) != len(set(acknowledgement_ids)):
            raise ValueError("acknowledgement IDs must be unique")


@dataclass(frozen=True, slots=True)
class MemoryRetentionDecision:
    """Explainable result of a pure retention check."""

    status: RetentionDecisionStatus
    record: MemoryRecordIdentity
    reason_codes: tuple[MemoryRetentionReason, ...]
    explanation: str
    active_job_reference_count: int
    active_deployment_reference_count: int
    backup_acknowledgement_count: int
    replica_acknowledgement_count: int

    @property
    def allowed(self) -> bool:
        return self.status is RetentionDecisionStatus.ALLOWED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "record": self.record.as_dict(),
            "reason_codes": [reason.value for reason in self.reason_codes],
            "explanation": self.explanation,
            "active_job_reference_count": self.active_job_reference_count,
            "active_deployment_reference_count": self.active_deployment_reference_count,
            "backup_acknowledgement_count": self.backup_acknowledgement_count,
            "replica_acknowledgement_count": self.replica_acknowledgement_count,
        }


_REASON_TEXT = {
    MemoryRetentionReason.RETENTION_NOT_EXPIRED: "retention window has not expired",
    MemoryRetentionReason.JOB_REFERENCE_IDENTITY_MISMATCH: "job reference identity does not match the target",
    MemoryRetentionReason.ACTIVE_JOB_REFERENCE: "an active job still references the target",
    MemoryRetentionReason.DEPLOYMENT_REFERENCE_IDENTITY_MISMATCH: "deployment reference identity does not match the target",
    MemoryRetentionReason.ACTIVE_DEPLOYMENT_REFERENCE: "an active deployment still references the target",
    MemoryRetentionReason.BACKUP_ACKNOWLEDGEMENT_MISMATCH: "backup acknowledgement evidence does not match the target",
    MemoryRetentionReason.BACKUP_ACKNOWLEDGEMENT_MISSING: "required backup acknowledgement is missing",
    MemoryRetentionReason.REPLICA_ACKNOWLEDGEMENT_MISMATCH: "replica acknowledgement evidence does not match the target",
    MemoryRetentionReason.REPLICA_ACKNOWLEDGEMENT_MISSING: "required replica acknowledgement is missing",
}


def _explanation(
    reasons: tuple[MemoryRetentionReason, ...],
    *,
    active_jobs: int,
    active_deployments: int,
    backups: int,
    replicas: int,
    required_replicas: int,
) -> str:
    if not reasons:
        text = (
            "deletion eligible: retention expired, no active references, and "
            "matching durability acknowledgements are present"
        )
    else:
        details = "; ".join(_REASON_TEXT[reason] for reason in reasons[:_MAX_REASON_CODES])
        counts = (
            f"counts jobs={active_jobs}, deployments={active_deployments}, "
            f"backups={backups}, replicas={replicas}/{required_replicas}"
        )
        text = f"deletion blocked: {details} ({counts})"
    return text[:_MAX_EXPLANATION_CHARS]


def decide_memory_retention(
    request: MemoryRetentionRequest,
    *,
    now: datetime | None = None,
) -> MemoryRetentionDecision:
    """Return a deterministic, fail-closed deletion eligibility decision.

    All references and acknowledgements are checked against the complete
    project/record/version/tombstone identity.  A mismatched evidence item is
    itself a blocking condition, so callers cannot accidentally treat proof
    from another project or revision as proof for this target.
    """

    if not isinstance(request, MemoryRetentionRequest):
        raise TypeError("request must be a MemoryRetentionRequest")
    current = _aware_utc(now or datetime.now(timezone.utc), "now")
    record = request.record
    reasons: list[MemoryRetentionReason] = []

    if not request.policy.expired(now=current):
        reasons.append(MemoryRetentionReason.RETENTION_NOT_EXPIRED)

    matching_jobs = tuple(
        reference
        for reference in request.active_job_references
        if reference.matches(record)
    )
    if len(matching_jobs) != len(request.active_job_references):
        reasons.append(MemoryRetentionReason.JOB_REFERENCE_IDENTITY_MISMATCH)
    if matching_jobs:
        reasons.append(MemoryRetentionReason.ACTIVE_JOB_REFERENCE)

    matching_deployments = tuple(
        reference
        for reference in request.active_deployment_references
        if reference.matches(record)
    )
    if len(matching_deployments) != len(request.active_deployment_references):
        reasons.append(MemoryRetentionReason.DEPLOYMENT_REFERENCE_IDENTITY_MISMATCH)
    if matching_deployments:
        reasons.append(MemoryRetentionReason.ACTIVE_DEPLOYMENT_REFERENCE)

    matching_backups = tuple(
        acknowledgement
        for acknowledgement in request.backup_acknowledgements
        if acknowledgement.kind is MemoryAcknowledgementKind.BACKUP
        and acknowledgement.matches(record)
    )
    if len(matching_backups) != len(request.backup_acknowledgements):
        reasons.append(MemoryRetentionReason.BACKUP_ACKNOWLEDGEMENT_MISMATCH)
    if request.policy.require_backup_acknowledgement and not matching_backups:
        reasons.append(MemoryRetentionReason.BACKUP_ACKNOWLEDGEMENT_MISSING)

    matching_replicas = tuple(
        acknowledgement
        for acknowledgement in request.replica_acknowledgements
        if acknowledgement.kind is MemoryAcknowledgementKind.REPLICA
        and acknowledgement.matches(record)
    )
    if len(matching_replicas) != len(request.replica_acknowledgements):
        reasons.append(MemoryRetentionReason.REPLICA_ACKNOWLEDGEMENT_MISMATCH)
    replica_holders = frozenset(item.holder_id for item in matching_replicas)
    if len(replica_holders) < request.policy.required_replica_acknowledgements:
        reasons.append(MemoryRetentionReason.REPLICA_ACKNOWLEDGEMENT_MISSING)

    reason_codes = tuple(dict.fromkeys(reasons))[:_MAX_REASON_CODES]
    status = (
        RetentionDecisionStatus.ALLOWED
        if not reason_codes
        else RetentionDecisionStatus.BLOCKED
    )
    return MemoryRetentionDecision(
        status=status,
        record=record,
        reason_codes=reason_codes,
        explanation=_explanation(
            reason_codes,
            active_jobs=len(matching_jobs),
            active_deployments=len(matching_deployments),
            backups=len(matching_backups),
            replicas=len(replica_holders),
            required_replicas=request.policy.required_replica_acknowledgements,
        ),
        active_job_reference_count=len(matching_jobs),
        active_deployment_reference_count=len(matching_deployments),
        backup_acknowledgement_count=len(matching_backups),
        replica_acknowledgement_count=len(replica_holders),
    )


__all__ = [
    "ActiveMemoryReference",
    "MemoryAcknowledgement",
    "MemoryAcknowledgementKind",
    "MemoryRecordIdentity",
    "MemoryRetentionDecision",
    "MemoryRetentionPolicy",
    "MemoryRetentionReason",
    "MemoryRetentionRequest",
    "RetentionDecisionStatus",
    "decide_memory_retention",
]
