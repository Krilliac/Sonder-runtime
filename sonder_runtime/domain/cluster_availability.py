"""Fail-closed availability profiles and provider-neutral control-state gates.

This module is deliberately a contract, not a cluster implementation.  The
single-PC profile keeps control state in the existing local SQLite graph.  The
two-PC profile describes a private resource pool, but it does not merge local
databases or promote a node.  A future adapter may satisfy the external
replication and fencing protocols below; it must present an acknowledgement
for durable data replicas and an independent fence receipt before a takeover
can be admitted.

The ownership fields mirror :mod:`sonder_runtime.domain.cluster_ownership`
(``cluster_id``, ``resource_kind``, ``resource_id``, ``owner_id`` and
``epoch``) without importing that optional implementation.  ``OwnershipScope``
can therefore be built from its ``OwnerLease`` while keeping this contract
usable on the single-host branch.

No class in this module opens a database, contacts a provider, elects a
coordinator, or claims high availability.  In particular, a witness is never
counted as a data replica and an ambiguous partition always fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Iterable, Protocol, runtime_checkable


CONTROL_STATE_PROTOCOL_VERSION = 1
MAX_PROFILE_MEMBERS = 16
MAX_REPLICA_IDS = 64
MAX_EVENT_SEQUENCE = (1 << 63) - 1
MAX_PAYLOAD_DIGEST_LENGTH = 64

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RESOURCE_KINDS = frozenset({"session", "attempt", "job", "approval", "memory_write"})


class AvailabilityProfileError(ValueError):
    """A topology, provider capability, or control-state contract is invalid."""


class AvailabilityProfile(StrEnum):
    """Explicit control-plane profiles supported by this contract."""

    SINGLE_PC = "single-pc"
    TWO_PC = "two-pc"


class ControlStateBackend(StrEnum):
    """Where the currently integrated control state is stored."""

    LOCAL_SQLITE = "local-sqlite"
    PER_NODE_LOCAL_SQLITE = "per-node-local-sqlite"


class PartitionState(StrEnum):
    """External fencing provider's view of the ownership partition."""

    SAFE = "safe"
    AMBIGUOUS = "ambiguous"
    MINORITY = "minority"
    UNAVAILABLE = "unavailable"


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise AvailabilityProfileError(f"{field} must be a bounded stable identity")
    return value


def _positive_integer(value: object, field: str, maximum: int = MAX_EVENT_SEQUENCE) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise AvailabilityProfileError(
            f"{field} must be an integer within 1..{maximum}"
        )
    return value


def _protocol_version(value: object, field: str = "protocol_version") -> int:
    return _positive_integer(value, field, 16)


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise AvailabilityProfileError(f"{field} must be a canonical SHA-256 digest")
    return value


def _identities(
    values: object,
    field: str,
    *,
    maximum: int = MAX_REPLICA_IDS,
) -> tuple[str, ...]:
    if type(values) is not tuple or not 0 <= len(values) <= maximum:
        raise AvailabilityProfileError(
            f"{field} must be an immutable tuple of at most {maximum} identities"
        )
    normalized = tuple(_identity(value, field) for value in values)
    if len(set(normalized)) != len(normalized):
        raise AvailabilityProfileError(f"{field} must not contain duplicates")
    return normalized


def _profile(value: object) -> AvailabilityProfile:
    if isinstance(value, AvailabilityProfile):
        return value
    if isinstance(value, str):
        aliases = {
            "single-host": AvailabilityProfile.SINGLE_PC,
            "pooled-pair": AvailabilityProfile.TWO_PC,
        }
        try:
            if value in aliases:
                return aliases[value]
            return AvailabilityProfile(value)
        except ValueError as exc:
            raise AvailabilityProfileError(
                "profile must be single-pc or two-pc"
            ) from exc
    raise AvailabilityProfileError("profile must be a supported availability profile")


@dataclass(frozen=True, slots=True)
class OwnershipScope:
    """The lease fields a provider must fence for one resource."""

    cluster_id: str
    resource_kind: str
    resource_id: str
    owner_id: str
    epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(
            self, "resource_kind", _identity(self.resource_kind, "resource_kind")
        )
        if self.resource_kind not in _RESOURCE_KINDS:
            raise AvailabilityProfileError("resource_kind is not supported")
        object.__setattr__(
            self, "resource_id", _identity(self.resource_id, "resource_id")
        )
        object.__setattr__(self, "owner_id", _identity(self.owner_id, "owner_id"))
        object.__setattr__(self, "epoch", _positive_integer(self.epoch, "epoch"))

    @property
    def owner_epoch(self) -> int:
        """Alias matching control-state event terminology."""
        return self.epoch

    @classmethod
    def from_lease(cls, lease: object) -> "OwnershipScope":
        """Adapt an existing ``OwnerLease`` without importing its module.

        This structural adapter keeps the ownership implementation and this
        provider contract independently deployable.  A malformed or unrelated
        object fails closed at the boundary.
        """
        if lease is None:
            raise AvailabilityProfileError("lease is required")
        fields = ("cluster_id", "resource_kind", "resource_id", "owner_id")
        try:
            values = [getattr(lease, field) for field in fields]
            epoch = getattr(lease, "epoch")
        except AttributeError as exc:
            raise AvailabilityProfileError("lease lacks scoped ownership fields") from exc
        return cls(*values, epoch)


@dataclass(frozen=True, slots=True)
class ControlStateEvent:
    """One versioned authoritative mutation eligible for replication."""

    event_id: str
    cluster_id: str
    resource_kind: str
    resource_id: str
    owner_id: str
    owner_epoch: int
    sequence: int
    payload_digest: str
    protocol_version: int = CONTROL_STATE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field in (
            "event_id",
            "cluster_id",
            "resource_id",
            "owner_id",
        ):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        object.__setattr__(
            self, "resource_kind", _identity(self.resource_kind, "resource_kind")
        )
        if self.resource_kind not in _RESOURCE_KINDS:
            raise AvailabilityProfileError("resource_kind is not supported")
        object.__setattr__(
            self,
            "owner_epoch",
            _positive_integer(self.owner_epoch, "owner_epoch"),
        )
        object.__setattr__(self, "sequence", _positive_integer(self.sequence, "sequence"))
        object.__setattr__(
            self, "payload_digest", _digest(self.payload_digest, "payload_digest")
        )
        object.__setattr__(
            self, "protocol_version", _protocol_version(self.protocol_version)
        )

    @property
    def scope(self) -> OwnershipScope:
        return OwnershipScope(
            self.cluster_id,
            self.resource_kind,
            self.resource_id,
            self.owner_id,
            self.owner_epoch,
        )


@dataclass(frozen=True, slots=True)
class ReplicatedControlStateCapabilities:
    """Declared external provider capabilities, never runtime evidence."""

    provider_id: str
    protocol_version: int = CONTROL_STATE_PROTOCOL_VERSION
    data_replica_ids: tuple[str, ...] = ()
    witness_ids: tuple[str, ...] = ()
    durable_acknowledgements: bool = False
    external_fencing: bool = False
    partition_policy: PartitionState = PartitionState.AMBIGUOUS

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _identity(self.provider_id, "provider_id"))
        object.__setattr__(
            self, "protocol_version", _protocol_version(self.protocol_version)
        )
        data = _identities(self.data_replica_ids, "data_replica_ids")
        witnesses = _identities(self.witness_ids, "witness_ids")
        if set(data) & set(witnesses):
            raise AvailabilityProfileError(
                "a provider identity cannot be both a data replica and a witness"
            )
        object.__setattr__(self, "data_replica_ids", data)
        object.__setattr__(self, "witness_ids", witnesses)
        for field in ("durable_acknowledgements", "external_fencing"):
            if type(getattr(self, field)) is not bool:
                raise AvailabilityProfileError(f"{field} must be boolean")
        if not isinstance(self.partition_policy, PartitionState):
            raise AvailabilityProfileError("partition_policy must be a PartitionState")

    @property
    def data_replica_count(self) -> int:
        return len(self.data_replica_ids)


@dataclass(frozen=True, slots=True)
class ReplicationAcknowledgement:
    """Durability receipt for one event and its data replicas."""

    event_id: str
    cluster_id: str
    owner_epoch: int
    sequence: int
    provider_id: str
    protocol_version: int
    data_replica_ids: tuple[str, ...]
    witness_ids: tuple[str, ...] = ()
    durable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _identity(self.event_id, "event_id"))
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(
            self, "owner_epoch", _positive_integer(self.owner_epoch, "owner_epoch")
        )
        object.__setattr__(self, "sequence", _positive_integer(self.sequence, "sequence"))
        object.__setattr__(self, "provider_id", _identity(self.provider_id, "provider_id"))
        object.__setattr__(
            self, "protocol_version", _protocol_version(self.protocol_version)
        )
        data = _identities(self.data_replica_ids, "data_replica_ids")
        witnesses = _identities(self.witness_ids, "witness_ids")
        if set(data) & set(witnesses):
            raise AvailabilityProfileError(
                "an acknowledgement identity cannot be both data and witness"
            )
        object.__setattr__(self, "data_replica_ids", data)
        object.__setattr__(self, "witness_ids", witnesses)
        if type(self.durable) is not bool:
            raise AvailabilityProfileError("durable must be boolean")

    @property
    def data_replica_count(self) -> int:
        return len(self.data_replica_ids)


@dataclass(frozen=True, slots=True)
class FenceReceipt:
    """Independent receipt that the previous owner was fenced."""

    receipt_id: str
    cluster_id: str
    resource_kind: str
    resource_id: str
    previous_owner_id: str
    previous_owner_epoch: int
    provider_id: str
    protocol_version: int
    partition_state: PartitionState
    external: bool
    accepted: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_id", _identity(self.receipt_id, "receipt_id"))
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(
            self, "resource_kind", _identity(self.resource_kind, "resource_kind")
        )
        if self.resource_kind not in _RESOURCE_KINDS:
            raise AvailabilityProfileError("resource_kind is not supported")
        object.__setattr__(
            self, "resource_id", _identity(self.resource_id, "resource_id")
        )
        object.__setattr__(
            self,
            "previous_owner_id",
            _identity(self.previous_owner_id, "previous_owner_id"),
        )
        object.__setattr__(
            self,
            "previous_owner_epoch",
            _positive_integer(self.previous_owner_epoch, "previous_owner_epoch"),
        )
        object.__setattr__(self, "provider_id", _identity(self.provider_id, "provider_id"))
        object.__setattr__(
            self, "protocol_version", _protocol_version(self.protocol_version)
        )
        if not isinstance(self.partition_state, PartitionState):
            raise AvailabilityProfileError("partition_state must be a PartitionState")
        for field in ("external", "accepted"):
            if type(getattr(self, field)) is not bool:
                raise AvailabilityProfileError(f"{field} must be boolean")


@dataclass(frozen=True, slots=True)
class ReplicationDecision:
    """Result of validating a durable data acknowledgement."""

    accepted: bool
    reason: str
    data_replica_count: int = 0


@dataclass(frozen=True, slots=True)
class TakeoverDecision:
    """Fail-closed takeover proof result; this does not mutate ownership."""

    allowed: bool
    reason: str
    next_epoch: int | None = None
    data_replica_count: int = 0


@runtime_checkable
class ReplicatedControlStateProvider(Protocol):
    """Adapter boundary for an established replicated control-state system."""

    protocol_version: int

    def append(self, event: ControlStateEvent) -> ReplicationAcknowledgement:
        """Durably append an event and return its replica acknowledgement."""

    def read(
        self,
        cluster_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 128,
    ) -> tuple[ControlStateEvent, ...]:
        """Read bounded authoritative events for recovery/reconciliation."""


@runtime_checkable
class OwnerFencingProvider(Protocol):
    """Adapter boundary for independent old-owner fencing."""

    protocol_version: int

    def fence(self, ownership: OwnershipScope) -> FenceReceipt:
        """Fence a scoped owner and return an independently verifiable receipt."""


def validate_replication_acknowledgement(
    event: ControlStateEvent,
    acknowledgement: ReplicationAcknowledgement,
    provider: ReplicatedControlStateCapabilities,
    *,
    minimum_data_replicas: int = 2,
) -> ReplicationDecision:
    """Validate the explicit data acknowledgement rule for one event.

    A successful result requires a durable acknowledgement for the exact
    event, epoch, and sequence, with at least two provider-admitted *data*
    replicas.  Witnesses are intentionally ignored for that count.  This
    function observes a provider descriptor; it does not contact or trust a
    provider based on configuration alone.
    """
    if not isinstance(event, ControlStateEvent):
        return ReplicationDecision(False, "invalid_event")
    if not isinstance(acknowledgement, ReplicationAcknowledgement):
        return ReplicationDecision(False, "invalid_acknowledgement")
    if not isinstance(provider, ReplicatedControlStateCapabilities):
        return ReplicationDecision(False, "invalid_provider")
    if type(minimum_data_replicas) is not int or not 1 <= minimum_data_replicas <= MAX_REPLICA_IDS:
        raise AvailabilityProfileError("minimum_data_replicas is outside the supported bound")
    if (
        event.protocol_version != acknowledgement.protocol_version
        or event.protocol_version != provider.protocol_version
    ):
        return ReplicationDecision(False, "protocol_version_mismatch")
    if acknowledgement.provider_id != provider.provider_id:
        return ReplicationDecision(False, "provider_mismatch")
    if acknowledgement.event_id != event.event_id or acknowledgement.cluster_id != event.cluster_id:
        return ReplicationDecision(False, "acknowledgement_mismatch")
    if (
        acknowledgement.owner_epoch != event.owner_epoch
        or acknowledgement.sequence != event.sequence
    ):
        return ReplicationDecision(False, "acknowledgement_mismatch")
    if not provider.durable_acknowledgements or not acknowledgement.durable:
        return ReplicationDecision(False, "durable_acknowledgement_required")
    if not set(acknowledgement.data_replica_ids) <= set(provider.data_replica_ids):
        return ReplicationDecision(False, "unrecognized_data_replica")
    count = acknowledgement.data_replica_count
    if count < minimum_data_replicas:
        return ReplicationDecision(False, "insufficient_data_replicas", count)
    return ReplicationDecision(True, "acknowledgement-accepted", count)


def evaluate_takeover(
    ownership: OwnershipScope,
    *,
    new_owner_id: str,
    event: ControlStateEvent,
    acknowledgement: ReplicationAcknowledgement,
    fence_receipt: FenceReceipt,
    provider: ReplicatedControlStateCapabilities | None,
    minimum_data_replicas: int = 2,
) -> TakeoverDecision:
    """Evaluate external takeover evidence without changing owner state.

    The decision is allowed only when the external provider declares durable
    replication and fencing, the exact prior epoch is acknowledged on enough
    data replicas, the receipt fences that exact owner, and the provider says
    the partition is safe.  A local timeout, a witness-only acknowledgement,
    or a two-node majority cannot satisfy this contract.
    """
    if not isinstance(ownership, OwnershipScope):
        return TakeoverDecision(False, "invalid_ownership")
    if provider is None:
        return TakeoverDecision(False, "external_provider_required")
    if not isinstance(provider, ReplicatedControlStateCapabilities):
        return TakeoverDecision(False, "invalid_provider")
    try:
        candidate_owner = _identity(new_owner_id, "new_owner_id")
    except AvailabilityProfileError:
        return TakeoverDecision(False, "invalid_new_owner")
    if candidate_owner == ownership.owner_id:
        return TakeoverDecision(False, "owner_must_change")
    if ownership.epoch >= MAX_EVENT_SEQUENCE:
        return TakeoverDecision(False, "epoch_exhausted")
    if not provider.external_fencing:
        return TakeoverDecision(False, "external_fencing_required")
    if provider.partition_policy is not PartitionState.SAFE:
        return TakeoverDecision(False, "partition_policy_unverified")
    if not isinstance(event, ControlStateEvent):
        return TakeoverDecision(False, "invalid_event")
    if event.scope != ownership:
        return TakeoverDecision(False, "ownership_mismatch")
    acknowledgement_result = validate_replication_acknowledgement(
        event,
        acknowledgement,
        provider,
        minimum_data_replicas=minimum_data_replicas,
    )
    if not acknowledgement_result.accepted:
        return TakeoverDecision(
            False,
            acknowledgement_result.reason,
            data_replica_count=acknowledgement_result.data_replica_count,
        )
    if not isinstance(fence_receipt, FenceReceipt):
        return TakeoverDecision(False, "invalid_fence_receipt")
    if (
        fence_receipt.protocol_version != event.protocol_version
        or fence_receipt.protocol_version != provider.protocol_version
    ):
        return TakeoverDecision(False, "protocol_version_mismatch")
    if fence_receipt.provider_id != provider.provider_id:
        return TakeoverDecision(False, "provider_mismatch")
    if not fence_receipt.external or not fence_receipt.accepted:
        return TakeoverDecision(False, "external_fence_receipt_required")
    if fence_receipt.partition_state is PartitionState.AMBIGUOUS:
        return TakeoverDecision(False, "ambiguous_partition")
    if fence_receipt.partition_state is PartitionState.MINORITY:
        return TakeoverDecision(False, "minority_partition")
    if fence_receipt.partition_state is not PartitionState.SAFE:
        return TakeoverDecision(False, "partition_state_unavailable")
    if (
        fence_receipt.cluster_id != ownership.cluster_id
        or fence_receipt.resource_kind != ownership.resource_kind
        or fence_receipt.resource_id != ownership.resource_id
        or fence_receipt.previous_owner_id != ownership.owner_id
        or fence_receipt.previous_owner_epoch != ownership.epoch
    ):
        return TakeoverDecision(False, "fence_scope_mismatch")
    return TakeoverDecision(
        True,
        "takeover-proof-satisfied",
        next_epoch=ownership.epoch + 1,
        data_replica_count=acknowledgement_result.data_replica_count,
    )


@dataclass(frozen=True, slots=True)
class AvailabilityProfileStatus:
    """Topology status with explicit capability and limitation reporting."""

    profile: AvailabilityProfile
    member_ids: tuple[str, ...]
    local_node_id: str
    preferred_primary: str
    control_state_scope: ControlStateBackend
    resource_pooling: bool
    provider_contract_valid: bool
    automatic_takeover_available: bool
    automatic_failback_available: bool
    takeover_mode: str
    partition_policy: str
    reasons: tuple[str, ...]
    guarantees: tuple[str, ...]
    limits: tuple[str, ...]

    @property
    def valid(self) -> bool:
        """Return whether the requested topology itself is valid."""
        return True

    def as_dict(self) -> dict[str, object]:
        """Return a safe capability report suitable for health/status output."""
        takeover_reason = {
            "disabled": "single-PC control state has no remote takeover path",
            "external-provider-required": (
                "two-PC pooling has no external replicated control-state provider"
            ),
            "protocol-version-mismatch": (
                "external provider protocol version does not match this contract"
            ),
            "external-provider-contract-invalid": (
                "external provider does not satisfy durable data and fencing prerequisites"
            ),
            "external-provider-proof-required": (
                "provider capability contract is present; exact fence and data evidence is still required"
            ),
        }.get(self.takeover_mode, "takeover is unavailable")
        return {
            "profile": self.profile.value,
            "configured_members": list(self.member_ids),
            "local_node": self.local_node_id,
            "preferred_primary": self.preferred_primary,
            "control_state_scope": self.control_state_scope.value,
            "resource_pooling": self.resource_pooling,
            "provider_contract_valid": self.provider_contract_valid,
            "partition_policy": self.partition_policy,
            "capabilities": {
                "automatic_takeover": {
                    "available": self.automatic_takeover_available,
                    "reason": takeover_reason,
                },
                "automatic_failback": {
                    "available": self.automatic_failback_available,
                    "reason": "automatic failback requires the same external provider proof",
                },
                "acknowledged_state_replication": {
                    "available": False,
                    "reason": "this contract does not instantiate or verify a replicated provider",
                },
                "worker_epoch_fencing": {
                    "available": False,
                    "reason": "provider evidence is required at each worker effect boundary",
                },
            },
            "reasons": list(self.reasons),
            "guarantees": list(self.guarantees),
            "limits": list(self.limits),
        }


def _bounded_members(members: Iterable[str]) -> tuple[str, ...]:
    if isinstance(members, (str, bytes)):
        raise AvailabilityProfileError("member_ids must be an iterable of identities")
    try:
        iterator = iter(members)
    except TypeError as exc:
        raise AvailabilityProfileError("member_ids must be an iterable of identities") from exc
    normalized: list[str] = []
    for value in iterator:
        if len(normalized) >= MAX_PROFILE_MEMBERS:
            raise AvailabilityProfileError(
                f"member_ids cannot contain more than {MAX_PROFILE_MEMBERS} members"
            )
        normalized.append(_identity(value, "member_id"))
    if len(set(normalized)) != len(normalized):
        raise AvailabilityProfileError("member_ids must not contain duplicates")
    return tuple(normalized)


def assess_availability_profile(
    profile: AvailabilityProfile | str,
    member_ids: Iterable[str],
    *,
    local_node_id: str,
    preferred_primary: str = "",
    provider: ReplicatedControlStateCapabilities | None = None,
    protocol_version: int = CONTROL_STATE_PROTOCOL_VERSION,
) -> AvailabilityProfileStatus:
    """Assess an explicit single-PC or two-PC topology.

    ``provider`` is a capability declaration for a future adapter.  Even when
    it satisfies the shape checks, ``automatic_takeover_available`` remains
    false because no live replication or fencing evidence was observed here.
    """
    selected = _profile(profile)
    members = _bounded_members(member_ids)
    local = _identity(local_node_id, "local_node_id")
    expected_protocol = _protocol_version(protocol_version)
    if local not in members:
        raise AvailabilityProfileError("local_node_id must name a configured member")
    if preferred_primary:
        preferred = _identity(preferred_primary, "preferred_primary")
        if preferred not in members:
            raise AvailabilityProfileError("preferred_primary must name a configured member")
    else:
        preferred = local

    if selected is AvailabilityProfile.SINGLE_PC:
        if len(members) != 1:
            raise AvailabilityProfileError("single-pc requires exactly one configured member")
        if provider is not None:
            raise AvailabilityProfileError(
                "single-pc keeps control state in local SQLite; external replication is not accepted"
            )
        return AvailabilityProfileStatus(
            profile=selected,
            member_ids=members,
            local_node_id=local,
            preferred_primary=preferred,
            control_state_scope=ControlStateBackend.LOCAL_SQLITE,
            resource_pooling=False,
            provider_contract_valid=False,
            automatic_takeover_available=False,
            automatic_failback_available=False,
            takeover_mode="disabled",
            partition_policy="pause-on-ambiguous-partition",
            reasons=("single-PC mode is local-only for authoritative control state",),
            guarantees=(
                "The configured PC may use its existing local SQLite control-state graph.",
                "No remote owner is promoted when this PC is unavailable.",
            ),
            limits=(
                "SQLite remains a single-host store and is not a replica.",
                "A local lease timeout cannot prove that another process or PC is fenced.",
            ),
        )

    if len(members) != 2:
        raise AvailabilityProfileError("two-pc requires exactly two configured members")
    reasons: list[str] = []
    provider_valid = False
    takeover_mode = "external-provider-required"
    if provider is None:
        reasons.append(
            "an external replicated control-state and fencing provider is required before takeover"
        )
    else:
        provider_errors: list[str] = []
        if provider.protocol_version != expected_protocol:
            provider_errors.append("provider protocol version mismatch")
        if not provider.durable_acknowledgements:
            provider_errors.append("provider does not declare durable acknowledgements")
        if not provider.external_fencing:
            provider_errors.append("provider does not declare external fencing")
        if provider.data_replica_count < 2:
            provider_errors.append("provider must declare at least two data replicas")
        if provider.partition_policy is not PartitionState.SAFE:
            provider_errors.append("provider partition policy is not fail-closed and safe")
        if provider_errors:
            reasons.extend(provider_errors)
            takeover_mode = (
                "protocol-version-mismatch"
                if provider.protocol_version != expected_protocol
                else "external-provider-contract-invalid"
            )
        else:
            provider_valid = True
            takeover_mode = "external-provider-proof-required"
            reasons.append(
                "provider capability shape is valid; live replication and fence receipts remain unverified"
            )
    return AvailabilityProfileStatus(
        profile=selected,
        member_ids=members,
        local_node_id=local,
        preferred_primary=preferred,
        control_state_scope=ControlStateBackend.PER_NODE_LOCAL_SQLITE,
        resource_pooling=True,
        provider_contract_valid=provider_valid,
        automatic_takeover_available=False,
        automatic_failback_available=False,
        takeover_mode=takeover_mode,
        partition_policy="pause-on-ambiguous-partition",
        reasons=tuple(reasons),
        guarantees=(
            "The two configured PCs may pool independently admitted private whole jobs.",
            "A preferred primary is an operator preference and grants no authority.",
        ),
        limits=(
            "Each PC keeps its own local SQLite control state until an external provider is integrated.",
            "A provider capability declaration does not claim live replication, fencing, or an HA guarantee.",
            "Ambiguous partitions must pause mutations; a witness does not replace a data copy.",
        ),
    )


def validate_availability_profile(
    profile: AvailabilityProfile | str,
    member_ids: Iterable[str],
    *,
    local_node_id: str,
    preferred_primary: str = "",
    provider: ReplicatedControlStateCapabilities | None = None,
    protocol_version: int = CONTROL_STATE_PROTOCOL_VERSION,
    require_provider_contract: bool = False,
) -> AvailabilityProfileStatus:
    """Validate topology and optionally require the external provider shape."""
    status = assess_availability_profile(
        profile,
        member_ids,
        local_node_id=local_node_id,
        preferred_primary=preferred_primary,
        provider=provider,
        protocol_version=protocol_version,
    )
    if (
        require_provider_contract
        and status.profile is AvailabilityProfile.TWO_PC
        and not status.provider_contract_valid
    ):
        raise AvailabilityProfileError(
            "external provider contract is required: " + "; ".join(status.reasons)
        )
    return status


__all__ = [
    "AvailabilityProfile",
    "AvailabilityProfileError",
    "AvailabilityProfileStatus",
    "CONTROL_STATE_PROTOCOL_VERSION",
    "ControlStateBackend",
    "ControlStateEvent",
    "FenceReceipt",
    "OwnerFencingProvider",
    "OwnershipScope",
    "PartitionState",
    "ReplicatedControlStateCapabilities",
    "ReplicatedControlStateProvider",
    "ReplicationAcknowledgement",
    "ReplicationDecision",
    "TakeoverDecision",
    "assess_availability_profile",
    "evaluate_takeover",
    "validate_availability_profile",
    "validate_replication_acknowledgement",
]
