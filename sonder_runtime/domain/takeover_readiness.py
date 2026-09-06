"""Pure, fail-closed evidence gate for takeover readiness.

This module answers one narrow question: *does a caller have enough bounded
evidence to ask an external authority to take over a resource?*  It does not
elect an owner, fence a process, replicate state, contact a witness, or mutate
runtime state.  ``ready`` is therefore an evidence result, never a promotion
command.

All three independent gates are required before readiness can be true:

* an explicit receipt from an authority that fenced the previous owner;
* an acknowledged durable state copy on the configured minimum number of
  distinct replicas; and
* a granted quorum decision from an authority and witness set independent of
  the owners and data replicas.

The evidence constructors only validate shape and bounds.  A provider remains
responsible for verifying signatures, process fencing, durable commits, and
quorum membership before it constructs the evidence values.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RESOURCE_KINDS = frozenset({"session", "attempt", "job", "approval", "memory_write"})
MAX_REPLICA_IDS = 64
MAX_WITNESS_IDS = 32
MAX_VOTES = 64
MAX_EPOCH = (1 << 63) - 1


class TakeoverTopology(StrEnum):
    """Deployment profiles whose limitations must remain visible to callers."""

    SINGLE_HOST = "single-host"
    TWO_NODE = "pooled-pair"
    MULTI_NODE = "multi-node"


class TakeoverReadinessReason(StrEnum):
    """Stable, low-cardinality explanations for a blocked gate."""

    READY = "ready"
    OLD_OWNER_FENCE_MISSING = "old_owner_fence_missing"
    OLD_OWNER_FENCE_SCOPE_MISMATCH = "old_owner_fence_scope_mismatch"
    OLD_OWNER_FENCE_UNCONFIRMED = "old_owner_fence_unconfirmed"
    OLD_OWNER_FENCE_NOT_INDEPENDENT = "old_owner_fence_not_independent"
    REPLICATION_EVIDENCE_MISSING = "replication_evidence_missing"
    REPLICATION_SCOPE_MISMATCH = "replication_scope_mismatch"
    REPLICATION_NOT_ACKNOWLEDGED = "replication_not_acknowledged"
    REPLICATION_NOT_DURABLE = "replication_not_durable"
    REPLICATION_QUORUM_NOT_REACHED = "replication_quorum_not_reached"
    REPLICATION_SOURCE_MISSING = "replication_source_missing"
    QUORUM_EVIDENCE_MISSING = "quorum_evidence_missing"
    QUORUM_SCOPE_MISMATCH = "quorum_scope_mismatch"
    QUORUM_DENIED = "quorum_denied"
    QUORUM_NOT_REACHED = "quorum_not_reached"
    QUORUM_NOT_INDEPENDENT = "quorum_not_independent"
    WITNESS_NOT_INDEPENDENT = "witness_not_independent"


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded stable identity")
    return value


def _resource_kind(value: object) -> str:
    if not isinstance(value, str) or value not in _RESOURCE_KINDS:
        raise ValueError("resource_kind is not supported")
    return value


def _epoch(value: object, field: str) -> int:
    if type(value) is not int or not 1 <= value <= MAX_EPOCH:
        raise ValueError(f"{field} must be within 1..{MAX_EPOCH}")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("state_digest must be a lowercase SHA-256 digest")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be boolean")
    return value


def _bounded_ids(value: object, field: str, *, maximum: int) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a bounded tuple of identities")
    try:
        values = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"{field} must be a bounded tuple of identities") from exc
    if not 1 <= len(values) <= maximum:
        raise ValueError(f"{field} must contain 1..{maximum} identities")
    normalized = tuple(_identity(item, f"{field} member") for item in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} must not contain duplicates")
    return normalized


def _receipt(value: object, field: str) -> str:
    return _identity(value, field)


def _topology(value: object) -> TakeoverTopology:
    try:
        return value if isinstance(value, TakeoverTopology) else TakeoverTopology(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("topology must be single-host, pooled-pair, or multi-node") from exc


@dataclass(frozen=True, slots=True)
class TakeoverRequest:
    """Identity of the resource and owner transition being assessed."""

    cluster_id: str
    resource_kind: str
    resource_id: str
    previous_owner_id: str
    previous_epoch: int
    new_owner_id: str
    topology: TakeoverTopology = TakeoverTopology.SINGLE_HOST

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "resource_kind", _resource_kind(self.resource_kind))
        object.__setattr__(self, "resource_id", _identity(self.resource_id, "resource_id"))
        object.__setattr__(self, "previous_owner_id", _identity(self.previous_owner_id, "previous_owner_id"))
        object.__setattr__(self, "previous_epoch", _epoch(self.previous_epoch, "previous_epoch"))
        object.__setattr__(self, "new_owner_id", _identity(self.new_owner_id, "new_owner_id"))
        object.__setattr__(self, "topology", _topology(self.topology))
        if self.previous_owner_id == self.new_owner_id:
            raise ValueError("new_owner_id must differ from previous_owner_id")


@dataclass(frozen=True, slots=True)
class OldOwnerFenceEvidence:
    """Immutable receipt proving an external authority fenced the old owner."""

    cluster_id: str
    resource_kind: str
    resource_id: str
    previous_owner_id: str
    previous_epoch: int
    receipt_id: str
    authority_id: str
    confirmed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "resource_kind", _resource_kind(self.resource_kind))
        object.__setattr__(self, "resource_id", _identity(self.resource_id, "resource_id"))
        object.__setattr__(self, "previous_owner_id", _identity(self.previous_owner_id, "previous_owner_id"))
        object.__setattr__(self, "previous_epoch", _epoch(self.previous_epoch, "previous_epoch"))
        object.__setattr__(self, "receipt_id", _receipt(self.receipt_id, "receipt_id"))
        object.__setattr__(self, "authority_id", _identity(self.authority_id, "authority_id"))
        object.__setattr__(self, "confirmed", _boolean(self.confirmed, "confirmed"))


@dataclass(frozen=True, slots=True)
class DurableStateReplicationEvidence:
    """Immutable provider receipt for a durable, acknowledged state set.

    ``replica_ids`` includes every distinct durable holder relevant to the
    takeover decision, including the previous owner's source copy.  A caller
    may require a larger count at evaluation time for a larger deployment.
    """

    cluster_id: str
    resource_kind: str
    resource_id: str
    previous_owner_id: str
    previous_epoch: int
    receipt_id: str
    state_digest: str
    acknowledged: bool
    durable: bool
    replica_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "resource_kind", _resource_kind(self.resource_kind))
        object.__setattr__(self, "resource_id", _identity(self.resource_id, "resource_id"))
        object.__setattr__(self, "previous_owner_id", _identity(self.previous_owner_id, "previous_owner_id"))
        object.__setattr__(self, "previous_epoch", _epoch(self.previous_epoch, "previous_epoch"))
        object.__setattr__(self, "receipt_id", _receipt(self.receipt_id, "receipt_id"))
        object.__setattr__(self, "state_digest", _digest(self.state_digest))
        object.__setattr__(self, "acknowledged", _boolean(self.acknowledged, "acknowledged"))
        object.__setattr__(self, "durable", _boolean(self.durable, "durable"))
        object.__setattr__(
            self,
            "replica_ids",
            _bounded_ids(self.replica_ids, "replica_ids", maximum=MAX_REPLICA_IDS),
        )


@dataclass(frozen=True, slots=True)
class IndependentQuorumDecision:
    """Immutable decision from an independent witness/quorum authority."""

    cluster_id: str
    resource_kind: str
    resource_id: str
    previous_owner_id: str
    previous_epoch: int
    decision_id: str
    authority_id: str
    witness_ids: tuple[str, ...]
    required_votes: int
    received_votes: int
    granted: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "resource_kind", _resource_kind(self.resource_kind))
        object.__setattr__(self, "resource_id", _identity(self.resource_id, "resource_id"))
        object.__setattr__(self, "previous_owner_id", _identity(self.previous_owner_id, "previous_owner_id"))
        object.__setattr__(self, "previous_epoch", _epoch(self.previous_epoch, "previous_epoch"))
        object.__setattr__(self, "decision_id", _receipt(self.decision_id, "decision_id"))
        object.__setattr__(self, "authority_id", _identity(self.authority_id, "authority_id"))
        object.__setattr__(
            self,
            "witness_ids",
            _bounded_ids(self.witness_ids, "witness_ids", maximum=MAX_WITNESS_IDS),
        )
        for name, value, minimum in (
            ("required_votes", self.required_votes, 1),
            ("received_votes", self.received_votes, 0),
        ):
            if type(value) is not int or not minimum <= value <= MAX_VOTES:
                raise ValueError(f"{name} must be within {minimum}..{MAX_VOTES}")
        object.__setattr__(self, "granted", _boolean(self.granted, "granted"))


@dataclass(frozen=True, slots=True)
class TakeoverReadinessDecision:
    """Auditable pure result; a ready result never performs promotion."""

    ready: bool
    profile: str
    cluster_id: str
    resource_kind: str
    resource_id: str
    previous_owner_id: str
    previous_epoch: int
    new_owner_id: str
    reason_codes: tuple[str, ...]
    limitations: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return not self.ready

    @property
    def reason(self) -> str:
        """Return a stable human-readable summary without evidence payloads."""

        return "; ".join(self.reason_codes)

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "profile": self.profile,
            "cluster_id": self.cluster_id,
            "resource_kind": self.resource_kind,
            "resource_id": self.resource_id,
            "previous_owner_id": self.previous_owner_id,
            "previous_epoch": self.previous_epoch,
            "new_owner_id": self.new_owner_id,
            "reason_codes": list(self.reason_codes),
            "limitations": list(self.limitations),
        }


def _scope_matches(request: TakeoverRequest, evidence: object) -> bool:
    return all(
        getattr(evidence, name) == getattr(request, name)
        for name in (
            "cluster_id",
            "resource_kind",
            "resource_id",
            "previous_owner_id",
            "previous_epoch",
        )
    )


def _limitations(topology: TakeoverTopology) -> tuple[str, ...]:
    common = (
        "This pure contract evaluates evidence only; it performs no election, network I/O, process fencing, or promotion.",
    )
    if topology is TakeoverTopology.SINGLE_HOST:
        return common + (
            "single-host supplies local control state only; an external replicated state provider and independent witness are required.",
        )
    if topology is TakeoverTopology.TWO_NODE:
        return common + (
            "pooled-pair has two data nodes; a third independent witness is required to prevent split-brain.",
        )
    return common + (
        "multi-node membership does not provide live fencing, replication transport, or quorum; those remain external provider capabilities.",
    )


def evaluate_takeover_readiness(
    request: TakeoverRequest,
    *,
    fence: OldOwnerFenceEvidence | None = None,
    replication: DurableStateReplicationEvidence | None = None,
    quorum: IndependentQuorumDecision | None = None,
    minimum_replicas: int = 2,
) -> TakeoverReadinessDecision:
    """Evaluate all takeover gates without performing any side effect.

    The default minimum of two durable holders models a source plus one
    distinct copy.  A higher bounded count can be selected for larger
    deployments.  Values below two cannot claim replicated takeover safety.
    """

    if not isinstance(request, TakeoverRequest):
        raise TypeError("request must be a TakeoverRequest")
    if type(minimum_replicas) is not int or not 2 <= minimum_replicas <= MAX_REPLICA_IDS:
        raise ValueError(f"minimum_replicas must be within 2..{MAX_REPLICA_IDS}")

    reasons: list[str] = []
    if fence is None:
        reasons.append(TakeoverReadinessReason.OLD_OWNER_FENCE_MISSING.value)
    elif not _scope_matches(request, fence):
        reasons.append(TakeoverReadinessReason.OLD_OWNER_FENCE_SCOPE_MISMATCH.value)
    else:
        if not fence.confirmed:
            reasons.append(TakeoverReadinessReason.OLD_OWNER_FENCE_UNCONFIRMED.value)
        if fence.authority_id in {request.previous_owner_id, request.new_owner_id}:
            reasons.append(TakeoverReadinessReason.OLD_OWNER_FENCE_NOT_INDEPENDENT.value)

    if replication is None:
        reasons.append(TakeoverReadinessReason.REPLICATION_EVIDENCE_MISSING.value)
    elif not _scope_matches(request, replication):
        reasons.append(TakeoverReadinessReason.REPLICATION_SCOPE_MISMATCH.value)
    else:
        if not replication.acknowledged:
            reasons.append(TakeoverReadinessReason.REPLICATION_NOT_ACKNOWLEDGED.value)
        if not replication.durable:
            reasons.append(TakeoverReadinessReason.REPLICATION_NOT_DURABLE.value)
        if len(replication.replica_ids) < minimum_replicas:
            reasons.append(TakeoverReadinessReason.REPLICATION_QUORUM_NOT_REACHED.value)
        if request.previous_owner_id not in replication.replica_ids:
            reasons.append(TakeoverReadinessReason.REPLICATION_SOURCE_MISSING.value)
        if (
            fence is not None
            and _scope_matches(request, fence)
            and fence.authority_id in replication.replica_ids
        ):
            reasons.append(TakeoverReadinessReason.OLD_OWNER_FENCE_NOT_INDEPENDENT.value)

    if quorum is None:
        reasons.append(TakeoverReadinessReason.QUORUM_EVIDENCE_MISSING.value)
    elif not _scope_matches(request, quorum):
        reasons.append(TakeoverReadinessReason.QUORUM_SCOPE_MISMATCH.value)
    else:
        if not quorum.granted:
            reasons.append(TakeoverReadinessReason.QUORUM_DENIED.value)
        if quorum.received_votes < quorum.required_votes or quorum.received_votes > len(quorum.witness_ids):
            reasons.append(TakeoverReadinessReason.QUORUM_NOT_REACHED.value)
        data_replicas = set(replication.replica_ids) if replication is not None else set()
        owners = {request.previous_owner_id, request.new_owner_id}
        if quorum.authority_id in owners or quorum.authority_id in data_replicas:
            reasons.append(TakeoverReadinessReason.QUORUM_NOT_INDEPENDENT.value)
        if set(quorum.witness_ids) & (owners | data_replicas):
            reasons.append(TakeoverReadinessReason.WITNESS_NOT_INDEPENDENT.value)

    if reasons:
        result_reasons = tuple(dict.fromkeys(reasons))
        return TakeoverReadinessDecision(
            ready=False,
            profile=request.topology.value,
            cluster_id=request.cluster_id,
            resource_kind=request.resource_kind,
            resource_id=request.resource_id,
            previous_owner_id=request.previous_owner_id,
            previous_epoch=request.previous_epoch,
            new_owner_id=request.new_owner_id,
            reason_codes=result_reasons,
            limitations=_limitations(request.topology),
        )
    return TakeoverReadinessDecision(
        ready=True,
        profile=request.topology.value,
        cluster_id=request.cluster_id,
        resource_kind=request.resource_kind,
        resource_id=request.resource_id,
        previous_owner_id=request.previous_owner_id,
        previous_epoch=request.previous_epoch,
        new_owner_id=request.new_owner_id,
        reason_codes=(TakeoverReadinessReason.READY.value,),
        limitations=_limitations(request.topology),
    )


__all__ = [
    "DurableStateReplicationEvidence",
    "IndependentQuorumDecision",
    "MAX_EPOCH",
    "MAX_REPLICA_IDS",
    "MAX_VOTES",
    "MAX_WITNESS_IDS",
    "OldOwnerFenceEvidence",
    "TakeoverReadinessDecision",
    "TakeoverReadinessReason",
    "TakeoverRequest",
    "TakeoverTopology",
    "evaluate_takeover_readiness",
]
