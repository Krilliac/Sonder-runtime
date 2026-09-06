"""Pure owner-epoch checks for worker effects.

The existing runtime effect fence can stop a worker whose owner lease is lost,
but an owner identity by itself is not enough when a resource is reclaimed by
the same worker identity at a newer epoch.  This module supplies the narrow
value and decision contract for carrying an epoch and token digest to an
effect boundary.

It deliberately performs no persistence, lease renewal, process inspection,
network I/O, or effect.  An adapter can obtain an
``OwnerEpochObservation`` from an authenticated owner record and wrap this
decision in the existing ``effect_fence.Fence`` or a job/capacity admission
check.  The observation must be refreshed by that adapter at its own safe
checkpoint; this pure function never assumes that a callback is current.

Reads remain explicitly unfenced to preserve the existing inspection and
reconciliation behavior.  Every mutating effect requires an active
observation with an exact scope, owner identity, owner epoch, and lease-token
digest match.  A stale or unverifiable observation fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_EPOCH = (1 << 63) - 1


class EffectKind(StrEnum):
    """Effect classes that can be attached to a worker operation."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    DELETE = "delete"
    CONTROL = "control"


class WorkerEffectReason(StrEnum):
    """Stable reasons returned by the effect fence decision."""

    CURRENT_OWNER = "current_owner"
    READ_UNFENCED = "read_unfenced"
    OWNER_EVIDENCE_MISSING = "owner_evidence_missing"
    OWNER_INACTIVE = "owner_inactive"
    OWNERSHIP_SCOPE_MISMATCH = "ownership_scope_mismatch"
    OWNER_MISMATCH = "owner_mismatch"
    STALE_OWNER_EPOCH = "stale_owner_epoch"
    OWNER_EPOCH_MISMATCH = "owner_epoch_mismatch"
    STALE_OWNER_TOKEN = "stale_owner_token"


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded stable identity")
    return value


def _epoch(value: object, field: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_EPOCH:
        raise ValueError(f"{field} must be within 1..{MAX_EPOCH}")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _effect(value: object) -> EffectKind:
    try:
        return value if isinstance(value, EffectKind) else EffectKind(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("effect must be read, write, execute, delete, or control") from exc


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class WorkerEffectRequest:
    """Expected ownership context attached to one worker operation."""

    cluster_id: str
    resource_kind: str
    resource_id: str
    owner_id: str
    owner_epoch: int
    lease_token_digest: str
    effect: EffectKind
    operation_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "resource_kind", _identity(self.resource_kind, "resource_kind"))
        object.__setattr__(self, "resource_id", _identity(self.resource_id, "resource_id"))
        object.__setattr__(self, "owner_id", _identity(self.owner_id, "owner_id"))
        object.__setattr__(self, "owner_epoch", _epoch(self.owner_epoch, "owner_epoch"))
        object.__setattr__(self, "lease_token_digest", _digest(self.lease_token_digest, "lease_token_digest"))
        object.__setattr__(self, "effect", _effect(self.effect))
        object.__setattr__(self, "operation_id", _identity(self.operation_id, "operation_id"))

    @property
    def scope_key(self) -> tuple[str, str, str]:
        """Stable key shared by capacity, job, and ownership adapters."""

        return self.cluster_id, self.resource_kind, self.resource_id


@dataclass(frozen=True, slots=True)
class OwnerEpochObservation:
    """Current owner state read by an adapter at an effect checkpoint."""

    cluster_id: str
    resource_kind: str
    resource_id: str
    owner_id: str
    owner_epoch: int
    lease_token_digest: str
    active: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "resource_kind", _identity(self.resource_kind, "resource_kind"))
        object.__setattr__(self, "resource_id", _identity(self.resource_id, "resource_id"))
        object.__setattr__(self, "owner_id", _identity(self.owner_id, "owner_id"))
        object.__setattr__(self, "owner_epoch", _epoch(self.owner_epoch, "owner_epoch"))
        object.__setattr__(self, "lease_token_digest", _digest(self.lease_token_digest, "lease_token_digest"))
        object.__setattr__(self, "active", _boolean(self.active, "active"))

    @property
    def scope_key(self) -> tuple[str, str, str]:
        return self.cluster_id, self.resource_kind, self.resource_id


@dataclass(frozen=True, slots=True)
class WorkerEffectDecision:
    """Bounded decision safe to pass to an effect adapter or status UI."""

    allowed: bool
    effect: EffectKind
    reason_code: str
    expected_epoch: int
    observed_epoch: int | None
    operation_id: str

    @property
    def blocked(self) -> bool:
        return not self.allowed

    @property
    def reason(self) -> str:
        return self.reason_code

    def as_dict(self) -> dict[str, object]:
        """Return a redacted status shape without lease-token material."""

        return {
            "allowed": self.allowed,
            "effect": self.effect.value,
            "reason_code": self.reason_code,
            "expected_epoch": self.expected_epoch,
            "observed_epoch": self.observed_epoch,
            "operation_id": self.operation_id,
        }


def _decision(
    request: WorkerEffectRequest,
    reason: WorkerEffectReason,
    *,
    allowed: bool,
    observed_epoch: int | None,
) -> WorkerEffectDecision:
    return WorkerEffectDecision(
        allowed=allowed,
        effect=request.effect,
        reason_code=reason.value,
        expected_epoch=request.owner_epoch,
        observed_epoch=observed_epoch,
        operation_id=request.operation_id,
    )


def evaluate_worker_effect(
    request: WorkerEffectRequest,
    observation: OwnerEpochObservation | None,
) -> WorkerEffectDecision:
    """Allow a worker effect only when its observed ownership is exact.

    The function is intentionally a snapshot check.  Callers performing an
    effect must obtain a fresh observation at their own authority boundary and
    must treat a blocked result as a refusal; this helper never retries or
    changes the owner.
    """

    if not isinstance(request, WorkerEffectRequest):
        raise TypeError("request must be a WorkerEffectRequest")
    if observation is not None and not isinstance(observation, OwnerEpochObservation):
        raise TypeError("observation must be an OwnerEpochObservation or None")

    if request.effect is EffectKind.READ:
        return _decision(
            request,
            WorkerEffectReason.READ_UNFENCED,
            allowed=True,
            observed_epoch=observation.owner_epoch if observation is not None else None,
        )
    if observation is None:
        return _decision(
            request,
            WorkerEffectReason.OWNER_EVIDENCE_MISSING,
            allowed=False,
            observed_epoch=None,
        )
    if observation.scope_key != request.scope_key:
        return _decision(
            request,
            WorkerEffectReason.OWNERSHIP_SCOPE_MISMATCH,
            allowed=False,
            observed_epoch=observation.owner_epoch,
        )
    if not observation.active:
        return _decision(
            request,
            WorkerEffectReason.OWNER_INACTIVE,
            allowed=False,
            observed_epoch=observation.owner_epoch,
        )
    if observation.owner_id != request.owner_id:
        return _decision(
            request,
            WorkerEffectReason.OWNER_MISMATCH,
            allowed=False,
            observed_epoch=observation.owner_epoch,
        )
    if observation.owner_epoch < request.owner_epoch:
        return _decision(
            request,
            WorkerEffectReason.STALE_OWNER_EPOCH,
            allowed=False,
            observed_epoch=observation.owner_epoch,
        )
    if observation.owner_epoch != request.owner_epoch:
        return _decision(
            request,
            WorkerEffectReason.OWNER_EPOCH_MISMATCH,
            allowed=False,
            observed_epoch=observation.owner_epoch,
        )
    if observation.lease_token_digest != request.lease_token_digest:
        return _decision(
            request,
            WorkerEffectReason.STALE_OWNER_TOKEN,
            allowed=False,
            observed_epoch=observation.owner_epoch,
        )
    return _decision(
        request,
        WorkerEffectReason.CURRENT_OWNER,
        allowed=True,
        observed_epoch=observation.owner_epoch,
    )


__all__ = [
    "EffectKind",
    "MAX_EPOCH",
    "OwnerEpochObservation",
    "WorkerEffectDecision",
    "WorkerEffectReason",
    "WorkerEffectRequest",
    "evaluate_worker_effect",
]
