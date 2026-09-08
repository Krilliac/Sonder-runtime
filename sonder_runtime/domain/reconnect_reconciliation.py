"""Provider-neutral client reconnect and worker-receipt reconciliation.

The runtime can discover several endpoints for one private cluster, but an
endpoint observation is not authority.  This module evaluates an explicit
discovery snapshot and an explicit owner epoch/lease binding, then produces a
bounded plan for a transport adapter.  It also evaluates worker receipts by
idempotency key so a reconnect can resume one known operation or replay one
terminal result without blindly submitting work again.

The policy is deliberately pure.  It performs no discovery I/O, network
connection, persistence, process control, consensus, failover, or promotion.
An adapter must supply the snapshot and receipts and must apply the returned
decision while retaining its own authorization and durable-state guarantees.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Iterable


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_EPOCH = (1 << 63) - 1
_MAX_REVISION = (1 << 63) - 1

MAX_DISCOVERY_MEMBERS = 256
MAX_PROTOCOL_VERSIONS = 16
MAX_RECEIPTS = 1024


class ReconnectContractError(ValueError):
    """Raised when a reconnect or receipt contract is malformed."""


class DiscoveryDisposition(StrEnum):
    """What a client may do with a discovery result."""

    CONNECTED = "connected"
    UNAVAILABLE = "unavailable"
    PAUSED = "paused"
    REJECTED = "rejected"


class ReceiptDisposition(StrEnum):
    """What an adapter may do with a reconciled worker receipt."""

    RESUME = "resume"
    REPLAY = "replay"
    UNAVAILABLE = "unavailable"
    PAUSED = "paused"
    REJECTED = "rejected"


class ReconnectReason(StrEnum):
    """Stable low-cardinality reasons suitable for status surfaces."""

    DISCOVERY_ACCEPTED = "discovery_accepted"
    MEMBER_UNAVAILABLE = "member_unavailable"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    CLUSTER_MISMATCH = "cluster_mismatch"
    AUTHORITY_EXPIRED = "authority_expired"
    AUTHORITY_STALE = "authority_stale"
    AUTHORITY_AHEAD = "authority_ahead"
    AUTHORITY_AMBIGUOUS = "authority_ambiguous"
    CLIENT_MISMATCH = "client_mismatch"
    RECEIPT_RESUMABLE = "receipt_resumable"
    RECEIPT_TERMINAL = "receipt_terminal"
    RECEIPT_NOT_FOUND = "receipt_not_found"
    RECEIPT_STALE = "receipt_stale"
    RECEIPT_AHEAD = "receipt_ahead"
    LEASE_MISMATCH = "lease_mismatch"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    RECEIPT_CONFLICT = "receipt_conflict"
    WORKER_PAUSED = "worker_paused"
    WORKER_INTERRUPTED = "worker_interrupted"


class WorkerReceiptState(StrEnum):
    """Bounded worker states understood by the reconciliation policy."""

    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {
            WorkerReceiptState.SUCCEEDED,
            WorkerReceiptState.FAILED,
            WorkerReceiptState.CANCELLED,
        }


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ReconnectContractError(f"{field} must be a bounded stable identity")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ReconnectContractError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _positive(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ReconnectContractError(f"{field} must be within 1..{maximum}")
    return value


def _nonnegative(value: object, field: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ReconnectContractError(f"{field} must be within 0..{maximum}")
    return value


def _boolean(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ReconnectContractError(f"{field} must be boolean")
    return value


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReconnectContractError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AuthorityLease:
    """The owner binding carried by every discovery and receipt snapshot."""

    cluster_id: str
    owner_id: str
    owner_epoch: int
    lease_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "owner_id", _identity(self.owner_id, "owner_id"))
        object.__setattr__(
            self,
            "owner_epoch",
            _positive(self.owner_epoch, "owner_epoch", _MAX_EPOCH),
        )
        object.__setattr__(self, "lease_id", _identity(self.lease_id, "lease_id"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))

    @property
    def binding(self) -> tuple[str, str, int, str]:
        """Return the identity fields; expiration is checked separately."""

        return self.cluster_id, self.owner_id, self.owner_epoch, self.lease_id

    def same_binding(self, other: object) -> bool:
        return isinstance(other, AuthorityLease) and self.binding == other.binding

    def live_at(self, now: datetime) -> bool:
        return _utc(now, "now") < self.expires_at

    def as_dict(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "owner_id": self.owner_id,
            "owner_epoch": self.owner_epoch,
            "lease_id": self.lease_id,
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True, slots=True)
class DiscoveryMember:
    """A transport-provided endpoint observation, not an authority grant."""

    node_id: str
    endpoint_id: str
    authority: AuthorityLease
    reachable: bool
    protocol_versions: tuple[int, ...] = (1,)

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _identity(self.node_id, "node_id"))
        object.__setattr__(self, "endpoint_id", _identity(self.endpoint_id, "endpoint_id"))
        if not isinstance(self.authority, AuthorityLease):
            raise ReconnectContractError("member authority must be an AuthorityLease")
        object.__setattr__(self, "reachable", _boolean(self.reachable, "reachable"))
        if (
            not isinstance(self.protocol_versions, tuple)
            or not self.protocol_versions
            or len(self.protocol_versions) > MAX_PROTOCOL_VERSIONS
            or any(type(version) is not int or not 1 <= version <= 16 for version in self.protocol_versions)
            or len(set(self.protocol_versions)) != len(self.protocol_versions)
        ):
            raise ReconnectContractError("protocol_versions must be bounded unique versions")
        if tuple(sorted(self.protocol_versions)) != self.protocol_versions:
            raise ReconnectContractError("protocol_versions must be sorted")

    def supports(self, protocol_version: int) -> bool:
        return protocol_version in self.protocol_versions

    def as_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "endpoint_id": self.endpoint_id,
            "authority": self.authority.as_dict(),
            "reachable": self.reachable,
            "protocol_versions": list(self.protocol_versions),
        }


@dataclass(frozen=True, slots=True)
class DiscoverySnapshot:
    """Bounded discovery data supplied by an adapter."""

    cluster_id: str
    authority: AuthorityLease
    revision: int
    members: tuple[DiscoveryMember, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        if not isinstance(self.authority, AuthorityLease):
            raise ReconnectContractError("snapshot authority must be an AuthorityLease")
        if self.authority.cluster_id != self.cluster_id:
            raise ReconnectContractError("snapshot authority cluster does not match snapshot")
        object.__setattr__(
            self,
            "revision",
            _positive(self.revision, "snapshot revision", _MAX_REVISION),
        )
        if (
            not isinstance(self.members, tuple)
            or len(self.members) > MAX_DISCOVERY_MEMBERS
            or any(not isinstance(member, DiscoveryMember) for member in self.members)
        ):
            raise ReconnectContractError("members must be a bounded tuple of DiscoveryMember values")
        node_ids = [member.node_id for member in self.members]
        endpoint_ids = [member.endpoint_id for member in self.members]
        if len(node_ids) != len(set(node_ids)):
            raise ReconnectContractError("discovery member node identities must be unique")
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ReconnectContractError("discovery endpoint identities must be unique")
        if any(member.authority.cluster_id != self.cluster_id for member in self.members):
            raise ReconnectContractError("discovery member authority cluster does not match snapshot")

    def as_dict(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "authority": self.authority.as_dict(),
            "revision": self.revision,
            "members": [member.as_dict() for member in self.members],
        }


@dataclass(frozen=True, slots=True)
class ClientReconnectRequest:
    """Stable client identity and its last known authority binding."""

    client_id: str
    cluster_id: str
    protocol_version: int
    last_authority: AuthorityLease | None = None
    session_id: str | None = None
    preferred_endpoint_id: str | None = None
    last_receipt_revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_id", _identity(self.client_id, "client_id"))
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(
            self,
            "protocol_version",
            _positive(self.protocol_version, "protocol_version", 16),
        )
        if self.last_authority is not None and not isinstance(self.last_authority, AuthorityLease):
            raise ReconnectContractError("last_authority must be an AuthorityLease or None")
        if self.session_id is not None:
            object.__setattr__(self, "session_id", _identity(self.session_id, "session_id"))
        if self.preferred_endpoint_id is not None:
            object.__setattr__(
                self,
                "preferred_endpoint_id",
                _identity(self.preferred_endpoint_id, "preferred_endpoint_id"),
            )
        object.__setattr__(
            self,
            "last_receipt_revision",
            _nonnegative(self.last_receipt_revision, "last_receipt_revision", _MAX_REVISION),
        )


@dataclass(frozen=True, slots=True)
class DiscoveryDecision:
    """A bounded, explainable endpoint-selection result."""

    disposition: DiscoveryDisposition
    reason: ReconnectReason
    selected_node_id: str | None
    selected_endpoint_id: str | None
    authority: AuthorityLease
    candidate_node_ids: tuple[str, ...]
    snapshot_revision: int

    @property
    def available(self) -> bool:
        return self.disposition is DiscoveryDisposition.CONNECTED

    @property
    def paused(self) -> bool:
        return self.disposition is DiscoveryDisposition.PAUSED

    @property
    def takeover_safe(self) -> bool:
        """Discovery evidence never grants authority to promote a node."""

        return False

    def as_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "reason": self.reason.value,
            "selected_node_id": self.selected_node_id,
            "selected_endpoint_id": self.selected_endpoint_id,
            "authority": self.authority.as_dict(),
            "candidate_node_ids": list(self.candidate_node_ids),
            "snapshot_revision": self.snapshot_revision,
            "takeover_safe": self.takeover_safe,
        }


@dataclass(frozen=True, slots=True)
class WorkerReceipt:
    """Immutable worker evidence for one idempotent operation attempt."""

    client_id: str
    cluster_id: str
    operation_id: str
    idempotency_key: str
    request_digest: str
    worker_id: str
    remote_job_id: str
    owner_id: str
    owner_epoch: int
    lease_id: str
    state: WorkerReceiptState
    revision: int
    output_watermark: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.client_id, "client_id"),
            (self.cluster_id, "cluster_id"),
            (self.operation_id, "operation_id"),
            (self.idempotency_key, "idempotency_key"),
            (self.worker_id, "worker_id"),
            (self.remote_job_id, "remote_job_id"),
            (self.owner_id, "owner_id"),
            (self.lease_id, "lease_id"),
        ):
            object.__setattr__(self, field, _identity(value, field))
        object.__setattr__(self, "request_digest", _digest(self.request_digest, "request_digest"))
        object.__setattr__(
            self,
            "owner_epoch",
            _positive(self.owner_epoch, "owner_epoch", _MAX_EPOCH),
        )
        try:
            object.__setattr__(self, "state", WorkerReceiptState(self.state))
        except (TypeError, ValueError) as exc:
            raise ReconnectContractError("state is not a recognized worker receipt state") from exc
        object.__setattr__(
            self,
            "revision",
            _positive(self.revision, "receipt revision", _MAX_REVISION),
        )
        object.__setattr__(
            self,
            "output_watermark",
            _nonnegative(self.output_watermark, "output_watermark", _MAX_REVISION),
        )

    @property
    def authority_binding(self) -> tuple[str, str, int, str]:
        return self.cluster_id, self.owner_id, self.owner_epoch, self.lease_id

    @property
    def receipt_identity(self) -> tuple[str, str]:
        return self.worker_id, self.remote_job_id

    def as_dict(self) -> dict[str, object]:
        return {
            "client_id": self.client_id,
            "cluster_id": self.cluster_id,
            "operation_id": self.operation_id,
            "idempotency_key": self.idempotency_key,
            "request_digest": self.request_digest,
            "worker_id": self.worker_id,
            "remote_job_id": self.remote_job_id,
            "owner_id": self.owner_id,
            "owner_epoch": self.owner_epoch,
            "lease_id": self.lease_id,
            "state": self.state.value,
            "revision": self.revision,
            "output_watermark": self.output_watermark,
        }


@dataclass(frozen=True, slots=True)
class ReceiptReconciliationRequest:
    """A client request to reconcile one exact operation identity."""

    client_id: str
    cluster_id: str
    operation_id: str
    idempotency_key: str
    request_digest: str
    authority: AuthorityLease
    last_seen_revision: int = 0

    def __post_init__(self) -> None:
        for value, field in (
            (self.client_id, "client_id"),
            (self.cluster_id, "cluster_id"),
            (self.operation_id, "operation_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            object.__setattr__(self, field, _identity(value, field))
        object.__setattr__(self, "request_digest", _digest(self.request_digest, "request_digest"))
        if not isinstance(self.authority, AuthorityLease):
            raise ReconnectContractError("authority must be an AuthorityLease")
        if self.authority.cluster_id != self.cluster_id:
            raise ReconnectContractError("request authority cluster does not match request")
        object.__setattr__(
            self,
            "last_seen_revision",
            _nonnegative(self.last_seen_revision, "last_seen_revision", _MAX_REVISION),
        )


@dataclass(frozen=True, slots=True)
class ReceiptReconciliationDecision:
    """A bounded receipt result that never grants worker or owner authority."""

    disposition: ReceiptDisposition
    reason: ReconnectReason
    receipt: WorkerReceipt | None
    candidate_count: int
    deduplicated_count: int

    @property
    def resume_allowed(self) -> bool:
        return self.disposition is ReceiptDisposition.RESUME

    @property
    def paused(self) -> bool:
        return self.disposition is ReceiptDisposition.PAUSED

    @property
    def unavailable(self) -> bool:
        return self.disposition is ReceiptDisposition.UNAVAILABLE

    @property
    def available(self) -> bool:
        return self.disposition in {
            ReceiptDisposition.RESUME,
            ReceiptDisposition.REPLAY,
        }

    @property
    def takeover_safe(self) -> bool:
        """A receipt plan never grants owner or process-fencing authority."""

        return False

    def as_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition.value,
            "reason": self.reason.value,
            "receipt": self.receipt.as_dict() if self.receipt is not None else None,
            "candidate_count": self.candidate_count,
            "deduplicated_count": self.deduplicated_count,
            "resume_allowed": self.resume_allowed,
            "takeover_safe": self.takeover_safe,
        }


def _authority_relation(expected: AuthorityLease, actual: AuthorityLease) -> ReconnectReason | None:
    """Compare a presented binding to a current binding without promoting either."""

    if expected.cluster_id != actual.cluster_id:
        return ReconnectReason.CLUSTER_MISMATCH
    if actual.owner_epoch < expected.owner_epoch:
        return ReconnectReason.AUTHORITY_STALE
    if actual.owner_epoch > expected.owner_epoch:
        return ReconnectReason.AUTHORITY_AHEAD
    if expected.owner_id != actual.owner_id or expected.lease_id != actual.lease_id:
        return ReconnectReason.AUTHORITY_AMBIGUOUS
    return None


def _receipt_authority_reason(receipt: WorkerReceipt, current: AuthorityLease) -> ReconnectReason | None:
    if receipt.cluster_id != current.cluster_id:
        return ReconnectReason.CLUSTER_MISMATCH
    if receipt.owner_epoch < current.owner_epoch:
        return ReconnectReason.RECEIPT_STALE
    if receipt.owner_epoch > current.owner_epoch:
        return ReconnectReason.RECEIPT_AHEAD
    if receipt.owner_id != current.owner_id or receipt.lease_id != current.lease_id:
        return ReconnectReason.LEASE_MISMATCH
    return None


class ReconnectReconciliationPolicy:
    """Evaluate endpoint discovery and exact worker receipt identities."""

    def __init__(
        self,
        *,
        supported_protocol_versions: tuple[int, ...] = (1,),
        max_members: int = MAX_DISCOVERY_MEMBERS,
        max_receipts: int = MAX_RECEIPTS,
    ) -> None:
        if (
            not isinstance(supported_protocol_versions, tuple)
            or not supported_protocol_versions
            or len(supported_protocol_versions) > MAX_PROTOCOL_VERSIONS
            or any(type(version) is not int or not 1 <= version <= 16 for version in supported_protocol_versions)
            or len(set(supported_protocol_versions)) != len(supported_protocol_versions)
            or tuple(sorted(supported_protocol_versions)) != supported_protocol_versions
        ):
            raise ReconnectContractError("supported_protocol_versions must be sorted bounded versions")
        if type(max_members) is not int or not 1 <= max_members <= MAX_DISCOVERY_MEMBERS:
            raise ReconnectContractError("max_members is outside the discovery bound")
        if type(max_receipts) is not int or not 1 <= max_receipts <= MAX_RECEIPTS:
            raise ReconnectContractError("max_receipts is outside the receipt bound")
        self.supported_protocol_versions = supported_protocol_versions
        self.max_members = max_members
        self.max_receipts = max_receipts

    def discover(
        self,
        request: ClientReconnectRequest,
        snapshot: DiscoverySnapshot,
        *,
        now: datetime,
    ) -> DiscoveryDecision:
        """Choose an endpoint only when the supplied authority remains current."""

        if not isinstance(request, ClientReconnectRequest):
            raise TypeError("request must be a ClientReconnectRequest")
        if not isinstance(snapshot, DiscoverySnapshot):
            raise TypeError("snapshot must be a DiscoverySnapshot")
        if len(snapshot.members) > self.max_members:
            raise ReconnectContractError("member bound exceeded")
        if request.cluster_id != snapshot.cluster_id:
            return self._discovery(
                DiscoveryDisposition.REJECTED,
                ReconnectReason.CLUSTER_MISMATCH,
                snapshot,
            )
        if request.protocol_version not in self.supported_protocol_versions:
            return self._discovery(
                DiscoveryDisposition.UNAVAILABLE,
                ReconnectReason.PROTOCOL_MISMATCH,
                snapshot,
            )
        if not snapshot.authority.live_at(now):
            return self._discovery(
                DiscoveryDisposition.PAUSED,
                ReconnectReason.AUTHORITY_EXPIRED,
                snapshot,
            )
        if request.last_authority is not None:
            reason = _authority_relation(request.last_authority, snapshot.authority)
            if reason is not None:
                return self._discovery(DiscoveryDisposition.PAUSED, reason, snapshot)

        current_members = [
            member
            for member in snapshot.members
            if member.authority.same_binding(snapshot.authority)
            and member.authority.live_at(now)
        ]
        reachable = [member for member in current_members if member.reachable]
        if not reachable:
            return self._discovery(
                DiscoveryDisposition.UNAVAILABLE,
                ReconnectReason.MEMBER_UNAVAILABLE,
                snapshot,
            )
        eligible = [
            member
            for member in reachable
            if member.supports(request.protocol_version)
        ]
        if not eligible:
            return self._discovery(
                DiscoveryDisposition.UNAVAILABLE,
                ReconnectReason.PROTOCOL_MISMATCH,
                snapshot,
            )
        eligible.sort(key=lambda member: (member.node_id, member.endpoint_id))
        selected = next(
            (
                member
                for member in eligible
                if member.endpoint_id == request.preferred_endpoint_id
            ),
            eligible[0],
        )
        return DiscoveryDecision(
            disposition=DiscoveryDisposition.CONNECTED,
            reason=ReconnectReason.DISCOVERY_ACCEPTED,
            selected_node_id=selected.node_id,
            selected_endpoint_id=selected.endpoint_id,
            authority=snapshot.authority,
            candidate_node_ids=tuple(member.node_id for member in eligible),
            snapshot_revision=snapshot.revision,
        )

    @staticmethod
    def _discovery(
        disposition: DiscoveryDisposition,
        reason: ReconnectReason,
        snapshot: DiscoverySnapshot,
    ) -> DiscoveryDecision:
        return DiscoveryDecision(
            disposition=disposition,
            reason=reason,
            selected_node_id=None,
            selected_endpoint_id=None,
            authority=snapshot.authority,
            candidate_node_ids=(),
            snapshot_revision=snapshot.revision,
        )

    def reconcile(
        self,
        request: ReceiptReconciliationRequest,
        receipts: Iterable[WorkerReceipt],
        *,
        current_authority: AuthorityLease,
        now: datetime,
    ) -> ReceiptReconciliationDecision:
        """Plan one safe resume/replay from bounded worker evidence.

        A matching idempotency key is a lookup identity, never permission to
        submit.  Distinct remote-job identities, stale owner epochs, lease
        mismatches, and same-revision conflicts all stop the plan explicitly.
        """

        if not isinstance(request, ReceiptReconciliationRequest):
            raise TypeError("request must be a ReceiptReconciliationRequest")
        if not isinstance(current_authority, AuthorityLease):
            raise TypeError("current_authority must be an AuthorityLease")
        if current_authority.cluster_id != request.cluster_id:
            return self._receipt_decision(
                ReceiptDisposition.REJECTED,
                ReconnectReason.CLUSTER_MISMATCH,
            )
        if not current_authority.live_at(now):
            return self._receipt_decision(
                ReceiptDisposition.PAUSED,
                ReconnectReason.AUTHORITY_EXPIRED,
            )
        authority_reason = _authority_relation(request.authority, current_authority)
        if authority_reason is not None:
            return self._receipt_decision(ReceiptDisposition.PAUSED, authority_reason)

        values: list[WorkerReceipt] = []
        try:
            iterator = iter(receipts)
        except TypeError as exc:
            raise ReconnectContractError("receipts must be iterable") from exc
        for index, receipt in enumerate(iterator):
            if index >= self.max_receipts:
                raise ReconnectContractError("receipt bound exceeded")
            if not isinstance(receipt, WorkerReceipt):
                raise ReconnectContractError("receipts must contain WorkerReceipt values")
            values.append(receipt)

        same_key = [
            receipt
            for receipt in values
            if receipt.cluster_id == request.cluster_id
            and receipt.idempotency_key == request.idempotency_key
        ]
        if any(
            receipt.client_id != request.client_id
            or receipt.operation_id != request.operation_id
            or receipt.request_digest != request.request_digest
            for receipt in same_key
        ):
            reason = (
                ReconnectReason.CLIENT_MISMATCH
                if any(receipt.client_id != request.client_id for receipt in same_key)
                else ReconnectReason.IDEMPOTENCY_CONFLICT
            )
            return self._receipt_decision(
                ReceiptDisposition.REJECTED,
                reason,
                candidate_count=len(same_key),
            )

        same_operation = [
            receipt
            for receipt in values
            if receipt.cluster_id == request.cluster_id
            and receipt.client_id == request.client_id
            and receipt.operation_id == request.operation_id
            and receipt.request_digest == request.request_digest
        ]
        if any(receipt.idempotency_key != request.idempotency_key for receipt in same_operation):
            return self._receipt_decision(
                ReceiptDisposition.REJECTED,
                ReconnectReason.IDEMPOTENCY_CONFLICT,
                candidate_count=len(same_operation),
            )
        if not same_key:
            return self._receipt_decision(
                ReceiptDisposition.UNAVAILABLE,
                ReconnectReason.RECEIPT_NOT_FOUND,
            )

        for receipt in same_key:
            reason = _receipt_authority_reason(receipt, current_authority)
            if reason is not None:
                return self._receipt_decision(
                    ReceiptDisposition.PAUSED,
                    reason,
                    candidate_count=len(same_key),
                )

        identities = {receipt.receipt_identity for receipt in same_key}
        if len(identities) != 1:
            return self._receipt_decision(
                ReceiptDisposition.REJECTED,
                ReconnectReason.RECEIPT_CONFLICT,
                candidate_count=len(same_key),
            )

        by_revision: dict[int, WorkerReceipt] = {}
        for receipt in same_key:
            prior = by_revision.get(receipt.revision)
            if prior is not None and prior != receipt:
                return self._receipt_decision(
                    ReceiptDisposition.REJECTED,
                    ReconnectReason.RECEIPT_CONFLICT,
                    candidate_count=len(same_key),
                )
            by_revision[receipt.revision] = receipt
        ordered = tuple(by_revision[key] for key in sorted(by_revision))
        if any(
            previous.output_watermark > current.output_watermark
            for previous, current in zip(ordered, ordered[1:])
        ):
            return self._receipt_decision(
                ReceiptDisposition.REJECTED,
                ReconnectReason.RECEIPT_CONFLICT,
                candidate_count=len(same_key),
                deduplicated_count=len(same_key) - len(by_revision),
            )
        selected = ordered[-1]
        deduplicated_count = len(same_key) - len(by_revision)
        if selected.revision < request.last_seen_revision:
            return self._receipt_decision(
                ReceiptDisposition.PAUSED,
                ReconnectReason.RECEIPT_STALE,
                receipt=selected,
                candidate_count=len(same_key),
                deduplicated_count=deduplicated_count,
            )
        if selected.state in {
            WorkerReceiptState.PENDING,
            WorkerReceiptState.CLAIMED,
            WorkerReceiptState.RUNNING,
        }:
            return self._receipt_decision(
                ReceiptDisposition.RESUME,
                ReconnectReason.RECEIPT_RESUMABLE,
                receipt=selected,
                candidate_count=len(same_key),
                deduplicated_count=deduplicated_count,
            )
        if selected.state is WorkerReceiptState.PAUSED:
            reason = ReconnectReason.WORKER_PAUSED
            disposition = ReceiptDisposition.PAUSED
        elif selected.state is WorkerReceiptState.INTERRUPTED:
            reason = ReconnectReason.WORKER_INTERRUPTED
            disposition = ReceiptDisposition.PAUSED
        else:
            reason = ReconnectReason.RECEIPT_TERMINAL
            disposition = ReceiptDisposition.REPLAY
        return self._receipt_decision(
            disposition,
            reason,
            receipt=selected,
            candidate_count=len(same_key),
            deduplicated_count=deduplicated_count,
        )

    @staticmethod
    def _receipt_decision(
        disposition: ReceiptDisposition,
        reason: ReconnectReason,
        *,
        receipt: WorkerReceipt | None = None,
        candidate_count: int = 0,
        deduplicated_count: int = 0,
    ) -> ReceiptReconciliationDecision:
        return ReceiptReconciliationDecision(
            disposition=disposition,
            reason=reason,
            receipt=receipt,
            candidate_count=candidate_count,
            deduplicated_count=deduplicated_count,
        )


__all__ = [
    "AuthorityLease",
    "ClientReconnectRequest",
    "DiscoveryDecision",
    "DiscoveryDisposition",
    "DiscoveryMember",
    "DiscoverySnapshot",
    "MAX_DISCOVERY_MEMBERS",
    "MAX_PROTOCOL_VERSIONS",
    "MAX_RECEIPTS",
    "ReceiptDisposition",
    "ReceiptReconciliationDecision",
    "ReceiptReconciliationRequest",
    "ReconnectContractError",
    "ReconnectReason",
    "ReconnectReconciliationPolicy",
    "WorkerReceipt",
    "WorkerReceiptState",
]
