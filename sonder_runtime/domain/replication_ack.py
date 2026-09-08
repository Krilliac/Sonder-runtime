"""Pure acknowledgement policy for provider-neutral durable replication.

The runtime can persist a control event in local SQLite without having a
replicated copy.  This module keeps those facts separate.  A replication
acknowledgement is accepted only when an immutable event envelope is matched
by a reachable, durable, authorized *data* replica that is distinct from the
source and current local node.  An arbitration witness can help a future
external authority make a decision, but it is never a data copy here.

This module deliberately performs no I/O, networking, consensus, takeover, or
fencing.  A provider may use the decision as one input after independently
implementing those concerns.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Iterable, Mapping


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MEMBERS = 1024
_MAX_ACKNOWLEDGEMENTS = 1024
_MAX_EVENT_PAYLOAD_BYTES = 64 * 1024
_MAX_EVENT_FIELDS = 64
_MAX_EPOCH = (1 << 63) - 1


class ReplicationMode(str, Enum):
    """The durability contract selected by a deployment."""

    LOCAL_SQLITE = "local_sqlite"
    LOCAL = "local_sqlite"
    POOLED_PAIR = "pooled_pair"
    TWO_PC_POOL = "pooled_pair"
    REPLICATED_DATA_QUORUM = "replicated_data_quorum"
    DATA_QUORUM = "replicated_data_quorum"


class ReplicaRole(str, Enum):
    """Whether a member stores data or only arbitrates authority."""

    DATA = "data"
    ARBITRATION_WITNESS = "arbitration_witness"
    WITNESS = "arbitration_witness"


class ReplicationState(str, Enum):
    """The conservative outcome of evaluating one event's evidence."""

    ACKNOWLEDGED = "acknowledged"
    LOCAL_ONLY = "local_only"
    PAUSED = "paused"
    UNAVAILABLE = "unavailable"


class ReplicationReason(str, Enum):
    """Stable, low-cardinality explanations for a decision."""

    LOCAL_SQLITE_ONLY = "local_sqlite_only"
    LOCAL_DURABILITY_MISSING = "local_durability_missing"
    DATA_REPLICA_NOT_CONFIGURED = "data_replica_not_configured"
    DATA_REPLICA_EVIDENCE_MISSING = "data_replica_evidence_missing"
    DATA_QUORUM_REACHED = "data_quorum_reached"
    DATA_QUORUM_NOT_REACHED = "data_quorum_not_reached"
    ARBITRATION_WITNESS_NOT_DATA = "arbitration_witness_not_data"
    REPLICA_NOT_AUTHORIZED = "replica_not_authorized"
    REPLICA_AUTHORIZATION_MISSING = "replica_not_authorized"
    REPLICA_NOT_DISTINCT = "replica_not_distinct"
    REPLICA_NOT_DATA = "replica_not_data"
    REPLICA_UNREACHABLE = "replica_unreachable"
    REPLICA_NOT_DURABLE = "replica_not_durable"
    CONFLICTING_REPLICA_EVIDENCE = "conflicting_replica_evidence"
    CLUSTER_MISMATCH = "cluster_mismatch"
    EVENT_ID_MISMATCH = "event_id_mismatch"
    SOURCE_NODE_MISMATCH = "source_node_mismatch"
    OWNER_ID_MISMATCH = "owner_id_mismatch"
    OWNER_EPOCH_MISMATCH = "owner_epoch_mismatch"
    LEASE_MISMATCH = "lease_mismatch"
    EVENT_DIGEST_MISMATCH = "event_digest_mismatch"
    PARTITION_PREVENTS_ACK = "partition_prevents_ack"


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded stable identity")
    return value


def _digest(value: object, field: str = "event_digest") -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _positive(value: object, field: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_EPOCH:
        raise ValueError(f"{field} must be within 1..{_MAX_EPOCH}")
    return value


def _strict_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be boolean")
    return value


def _members(value: object, field: str) -> frozenset[str]:
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a bounded collection of identities")
    try:
        values = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"{field} must be a bounded collection of identities") from exc
    if len(values) > _MAX_MEMBERS:
        raise ValueError(f"{field} exceeds the member bound")
    normalized = frozenset(_identity(item, f"{field} member") for item in values)
    if len(normalized) != len(values):
        raise ValueError(f"{field} must not contain duplicates")
    return normalized


def event_digest_for(
    *,
    cluster_id: str,
    event_id: str,
    source_node_id: str,
    owner_id: str,
    owner_epoch: int,
    lease_id: str,
    payload: Mapping[str, object] | None = None,
) -> str:
    """Hash a bounded canonical event envelope for :class:`ControlEvent`.

    The function is deterministic and has no provider or transport semantics.
    Callers should persist the returned digest with the event and include that
    exact value in every replica receipt.
    """

    cluster = _identity(cluster_id, "cluster_id")
    event = _identity(event_id, "event_id")
    source = _identity(source_node_id, "source_node_id")
    owner = _identity(owner_id, "owner_id")
    epoch = _positive(owner_epoch, "owner_epoch")
    lease = _identity(lease_id, "lease_id")
    if payload is None:
        normalized_payload: Mapping[str, object] = {}
    elif not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    else:
        if len(payload) > _MAX_EVENT_FIELDS:
            raise ValueError("payload has too many fields")
        normalized_payload = MappingProxyType(dict(payload))
    try:
        encoded = json.dumps(
            {
                "schema": "sonder.control-event.v1",
                "cluster_id": cluster,
                "event_id": event,
                "source_node_id": source,
                "owner_id": owner,
                "owner_epoch": epoch,
                "lease_id": lease,
                "payload": dict(normalized_payload),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must contain bounded JSON values") from exc
    if len(encoded) > _MAX_EVENT_PAYLOAD_BYTES:
        raise ValueError("event envelope exceeds the replication bound")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ControlEvent:
    """Immutable identity of the control event being acknowledged."""

    cluster_id: str
    event_id: str
    source_node_id: str
    owner_id: str
    owner_epoch: int
    lease_id: str
    event_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "event_id", _identity(self.event_id, "event_id"))
        object.__setattr__(self, "source_node_id", _identity(self.source_node_id, "source_node_id"))
        object.__setattr__(self, "owner_id", _identity(self.owner_id, "owner_id"))
        object.__setattr__(self, "owner_epoch", _positive(self.owner_epoch, "owner_epoch"))
        object.__setattr__(self, "lease_id", _identity(self.lease_id, "lease_id"))
        object.__setattr__(self, "event_digest", _digest(self.event_digest))

    @classmethod
    def from_payload(
        cls,
        *,
        cluster_id: str,
        event_id: str,
        source_node_id: str,
        owner_id: str,
        owner_epoch: int,
        lease_id: str,
        payload: Mapping[str, object] | None = None,
    ) -> "ControlEvent":
        """Build an event whose digest is computed from its canonical envelope."""

        return cls(
            cluster_id=cluster_id,
            event_id=event_id,
            source_node_id=source_node_id,
            owner_id=owner_id,
            owner_epoch=owner_epoch,
            lease_id=lease_id,
            event_digest=event_digest_for(
                cluster_id=cluster_id,
                event_id=event_id,
                source_node_id=source_node_id,
                owner_id=owner_id,
                owner_epoch=owner_epoch,
                lease_id=lease_id,
                payload=payload,
            ),
        )


@dataclass(frozen=True, slots=True)
class ReplicaAcknowledgement:
    """One immutable receipt from a potential replica.

    ``authorized`` is an assertion carried by the provider, while membership
    in the policy's ``authorized_data_replica_ids`` is the local admission
    control.  Both must be true.  ``durable`` means the provider has completed
    its own durable write; this module never performs or verifies that write.
    """

    acknowledgement_id: str
    cluster_id: str
    event_id: str
    source_node_id: str
    replica_id: str
    role: ReplicaRole
    authorized: bool
    reachable: bool
    durable: bool
    owner_id: str
    owner_epoch: int
    lease_id: str
    event_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "acknowledgement_id", _identity(self.acknowledgement_id, "acknowledgement_id"))
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "event_id", _identity(self.event_id, "event_id"))
        object.__setattr__(self, "source_node_id", _identity(self.source_node_id, "source_node_id"))
        object.__setattr__(self, "replica_id", _identity(self.replica_id, "replica_id"))
        try:
            object.__setattr__(self, "role", ReplicaRole(self.role))
        except (TypeError, ValueError) as exc:
            raise ValueError("role must be data or arbitration_witness") from exc
        object.__setattr__(self, "authorized", _strict_bool(self.authorized, "authorized"))
        object.__setattr__(self, "reachable", _strict_bool(self.reachable, "reachable"))
        object.__setattr__(self, "durable", _strict_bool(self.durable, "durable"))
        object.__setattr__(self, "owner_id", _identity(self.owner_id, "owner_id"))
        object.__setattr__(self, "owner_epoch", _positive(self.owner_epoch, "owner_epoch"))
        object.__setattr__(self, "lease_id", _identity(self.lease_id, "lease_id"))
        object.__setattr__(self, "event_digest", _digest(self.event_digest))

    @classmethod
    def for_event(
        cls,
        event: ControlEvent,
        replica_id: str,
        *,
        acknowledgement_id: str | None = None,
        role: ReplicaRole = ReplicaRole.DATA,
        authorized: bool = True,
        reachable: bool = True,
        durable: bool = True,
    ) -> "ReplicaAcknowledgement":
        """Create a receipt carrying the event's complete immutable identity."""

        if not isinstance(event, ControlEvent):
            raise ValueError("event must be a ControlEvent")
        if acknowledgement_id is None:
            acknowledgement_id = "ack-" + hashlib.sha256(
                f"{event.event_id}:{replica_id}".encode("utf-8")
            ).hexdigest()[:32]
        return cls(
            acknowledgement_id=acknowledgement_id,
            cluster_id=event.cluster_id,
            event_id=event.event_id,
            source_node_id=event.source_node_id,
            replica_id=replica_id,
            role=role,
            authorized=authorized,
            reachable=reachable,
            durable=durable,
            owner_id=event.owner_id,
            owner_epoch=event.owner_epoch,
            lease_id=event.lease_id,
            event_digest=event.event_digest,
        )

    @property
    def fingerprint(self) -> tuple[object, ...]:
        """Evidence identity excluding the retry-specific receipt ID."""

        return (
            self.cluster_id,
            self.event_id,
            self.source_node_id,
            self.replica_id,
            self.role,
            self.authorized,
            self.reachable,
            self.durable,
            self.owner_id,
            self.owner_epoch,
            self.lease_id,
            self.event_digest,
        )


@dataclass(frozen=True, slots=True)
class ReplicationDecision:
    """Conservative, explainable result of one policy evaluation."""

    mode: ReplicationMode
    state: ReplicationState
    acknowledged: bool
    local_durable: bool
    data_replica_count: int
    required_data_replicas: int
    replica_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    event_digest: str
    owner_epoch: int
    lease_id: str

    @property
    def replicated(self) -> bool:
        """True only when the configured data-copy requirement is met."""

        return self.acknowledged

    @property
    def available(self) -> bool:
        return self.state is ReplicationState.ACKNOWLEDGED

    @property
    def paused(self) -> bool:
        return self.state is ReplicationState.PAUSED

    @property
    def unavailable(self) -> bool:
        return self.state is ReplicationState.UNAVAILABLE

    @property
    def takeover_safe(self) -> bool:
        """Replication evidence alone never grants ownership authority."""

        return False

    def as_dict(self) -> dict[str, object]:
        """Return a redacted status payload suitable for a health surface."""

        return {
            "mode": self.mode.value,
            "state": self.state.value,
            "acknowledged": self.acknowledged,
            "replicated": self.replicated,
            "local_durable": self.local_durable,
            "data_replica_count": self.data_replica_count,
            "required_data_replicas": self.required_data_replicas,
            "replica_ids": list(self.replica_ids),
            "reason_codes": list(self.reason_codes),
            "event_digest": self.event_digest,
            "owner_epoch": self.owner_epoch,
            "lease_id": self.lease_id,
            "takeover_safe": self.takeover_safe,
        }


@dataclass(frozen=True, slots=True)
class ReplicationAcknowledgementPolicy:
    """Pure admission policy for local, pair, or data-quorum deployments.

    ``authorized_data_replica_ids`` names remote members trusted to store
    durable copies.  ``arbitration_witness_ids`` is metadata for members that
    may arbitrate a future external authority; it is intentionally excluded
    from the data-copy count.
    """

    mode: ReplicationMode
    local_node_id: str
    authorized_data_replica_ids: frozenset[str] = frozenset()
    arbitration_witness_ids: frozenset[str] = frozenset()
    required_data_replicas: int | None = None

    def __post_init__(self) -> None:
        try:
            mode = ReplicationMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("mode must be local_sqlite, pooled_pair, or replicated_data_quorum") from exc
        object.__setattr__(self, "mode", mode)
        local = _identity(self.local_node_id, "local_node_id")
        object.__setattr__(self, "local_node_id", local)
        data_ids = _members(self.authorized_data_replica_ids, "authorized_data_replica_ids")
        witness_ids = _members(self.arbitration_witness_ids, "arbitration_witness_ids")
        if local in data_ids or local in witness_ids:
            raise ValueError("local node cannot be listed as a replica or witness")
        if data_ids & witness_ids:
            raise ValueError("data replicas and arbitration witnesses must be disjoint")
        object.__setattr__(self, "authorized_data_replica_ids", data_ids)
        object.__setattr__(self, "arbitration_witness_ids", witness_ids)

        required = self.required_data_replicas
        if required is None:
            required = {
                ReplicationMode.LOCAL_SQLITE: 0,
                ReplicationMode.POOLED_PAIR: 1,
                ReplicationMode.REPLICATED_DATA_QUORUM: 2,
            }[mode]
        if type(required) is not int or required < 0 or required > _MAX_MEMBERS:
            raise ValueError("required_data_replicas must be a bounded non-negative integer")
        if mode is ReplicationMode.LOCAL_SQLITE and required != 0:
            raise ValueError("local_sqlite requires required_data_replicas=0")
        if mode is ReplicationMode.POOLED_PAIR and required != 1:
            raise ValueError("pooled_pair requires required_data_replicas=1")
        if mode is ReplicationMode.REPLICATED_DATA_QUORUM and required < 2:
            raise ValueError("replicated_data_quorum requires required_data_replicas>=2")
        object.__setattr__(self, "required_data_replicas", required)

    def evaluate(
        self,
        event: ControlEvent,
        acknowledgements: Iterable[ReplicaAcknowledgement] = (),
        *,
        local_durable: bool | None = None,
        local_commit_durable: bool | None = None,
        partitioned: bool = False,
    ) -> ReplicationDecision:
        """Evaluate evidence without contacting any provider.

        ``local_durable`` is the local SQLite commit result.  It is kept
        separate from a replicated acknowledgement.  ``local_commit_durable``
        is an explicit spelling alias for callers that use transaction terms;
        supplying both values with different meanings is rejected.
        """

        if not isinstance(event, ControlEvent):
            raise ValueError("event must be a ControlEvent")
        if local_durable is None:
            local_durable = local_commit_durable if local_commit_durable is not None else False
        elif local_commit_durable is not None and local_durable != local_commit_durable:
            raise ValueError("local_durable and local_commit_durable disagree")
        local_durable = _strict_bool(local_durable, "local_durable")
        partitioned = _strict_bool(partitioned, "partitioned")
        try:
            receipts = tuple(acknowledgements)
        except TypeError as exc:
            raise ValueError("acknowledgements must be a bounded collection") from exc
        if len(receipts) > _MAX_ACKNOWLEDGEMENTS:
            raise ValueError("acknowledgements exceed the evidence bound")
        if any(not isinstance(ack, ReplicaAcknowledgement) for ack in receipts):
            raise ValueError("acknowledgements must contain ReplicaAcknowledgement values")

        reasons: list[str] = []

        def add(reason: ReplicationReason) -> None:
            value = reason.value
            if value not in reasons:
                reasons.append(value)

        if not local_durable:
            add(ReplicationReason.LOCAL_DURABILITY_MISSING)

        required = int(self.required_data_replicas)
        if self.mode is ReplicationMode.LOCAL_SQLITE:
            add(ReplicationReason.LOCAL_SQLITE_ONLY)
            return self._decision(
                event,
                mode=self.mode,
                state=(ReplicationState.LOCAL_ONLY if local_durable else ReplicationState.UNAVAILABLE),
                acknowledged=False,
                local_durable=local_durable,
                data_replica_count=0,
                required=required,
                replica_ids=(),
                reasons=reasons,
            )

        if not self.authorized_data_replica_ids:
            add(ReplicationReason.DATA_REPLICA_NOT_CONFIGURED)
            return self._decision(
                event,
                mode=self.mode,
                state=ReplicationState.PAUSED if not local_durable else ReplicationState.UNAVAILABLE,
                acknowledged=False,
                local_durable=local_durable,
                data_replica_count=0,
                required=required,
                replica_ids=(),
                reasons=reasons,
            )

        by_replica: dict[str, list[ReplicaAcknowledgement]] = {}
        for ack in receipts:
            by_replica.setdefault(ack.replica_id, []).append(ack)

        valid_ids: list[str] = []
        saw_unreachable = False
        for replica_id, grouped in by_replica.items():
            fingerprints = {ack.fingerprint for ack in grouped}
            if len(fingerprints) != 1:
                add(ReplicationReason.CONFLICTING_REPLICA_EVIDENCE)
                continue
            ack = grouped[0]
            valid = True
            if ack.cluster_id != event.cluster_id:
                add(ReplicationReason.CLUSTER_MISMATCH)
                valid = False
            if ack.event_id != event.event_id:
                add(ReplicationReason.EVENT_ID_MISMATCH)
                valid = False
            if ack.source_node_id != event.source_node_id:
                add(ReplicationReason.SOURCE_NODE_MISMATCH)
                valid = False
            if ack.owner_id != event.owner_id:
                add(ReplicationReason.OWNER_ID_MISMATCH)
                valid = False
            if ack.owner_epoch != event.owner_epoch:
                add(ReplicationReason.OWNER_EPOCH_MISMATCH)
                valid = False
            if ack.lease_id != event.lease_id:
                add(ReplicationReason.LEASE_MISMATCH)
                valid = False
            if ack.event_digest != event.event_digest:
                add(ReplicationReason.EVENT_DIGEST_MISMATCH)
                valid = False
            if replica_id == self.local_node_id or replica_id == event.source_node_id:
                add(ReplicationReason.REPLICA_NOT_DISTINCT)
                valid = False
            if replica_id in self.arbitration_witness_ids or ack.role is ReplicaRole.ARBITRATION_WITNESS:
                add(ReplicationReason.ARBITRATION_WITNESS_NOT_DATA)
                valid = False
            if replica_id not in self.authorized_data_replica_ids:
                add(ReplicationReason.REPLICA_NOT_AUTHORIZED)
                valid = False
            if ack.role is not ReplicaRole.DATA:
                add(ReplicationReason.REPLICA_NOT_DATA)
                valid = False
            if not ack.authorized:
                add(ReplicationReason.REPLICA_NOT_AUTHORIZED)
                valid = False
            if not ack.reachable:
                add(ReplicationReason.REPLICA_UNREACHABLE)
                saw_unreachable = True
                valid = False
            if not ack.durable:
                add(ReplicationReason.REPLICA_NOT_DURABLE)
                valid = False
            if valid:
                valid_ids.append(replica_id)

        valid_ids.sort()
        count = len(valid_ids)
        if count == 0:
            add(ReplicationReason.DATA_REPLICA_EVIDENCE_MISSING)
        if count >= required and required > 0:
            add(ReplicationReason.DATA_QUORUM_REACHED)
        elif count > 0:
            add(ReplicationReason.DATA_QUORUM_NOT_REACHED)
        if partitioned or saw_unreachable:
            add(ReplicationReason.PARTITION_PREVENTS_ACK)

        if not local_durable:
            state = ReplicationState.PAUSED
            acknowledged = False
        elif count >= required and required > 0 and not (partitioned or saw_unreachable):
            state = ReplicationState.ACKNOWLEDGED
            acknowledged = True
        elif partitioned or saw_unreachable or count > 0:
            state = ReplicationState.PAUSED
            acknowledged = False
        else:
            state = ReplicationState.UNAVAILABLE
            acknowledged = False

        return self._decision(
            event,
            mode=self.mode,
            state=state,
            acknowledged=acknowledged,
            local_durable=local_durable,
            data_replica_count=count,
            required=required,
            replica_ids=tuple(valid_ids),
            reasons=reasons,
        )

    def assess(
        self,
        event: ControlEvent,
        acknowledgements: Iterable[ReplicaAcknowledgement] = (),
        **kwargs: object,
    ) -> ReplicationDecision:
        """Alias for :meth:`evaluate` used by status/health callers."""

        return self.evaluate(event, acknowledgements, **kwargs)  # type: ignore[arg-type]

    @staticmethod
    def _decision(
        event: ControlEvent,
        *,
        mode: ReplicationMode,
        state: ReplicationState,
        acknowledged: bool,
        local_durable: bool,
        data_replica_count: int,
        required: int,
        replica_ids: tuple[str, ...],
        reasons: list[str],
    ) -> ReplicationDecision:
        return ReplicationDecision(
            mode=mode,
            state=state,
            acknowledged=acknowledged,
            local_durable=local_durable,
            data_replica_count=data_replica_count,
            required_data_replicas=required,
            replica_ids=replica_ids,
            reason_codes=tuple(reasons),
            event_digest=event.event_digest,
            owner_epoch=event.owner_epoch,
            lease_id=event.lease_id,
        )


# Concise public spellings for callers that use US terminology.  They point to
# the same implementation and do not add another compatibility execution path.
ReplicationPolicy = ReplicationAcknowledgementPolicy
ReplicationAcknowledgmentPolicy = ReplicationAcknowledgementPolicy
ReplicaAcknowledgment = ReplicaAcknowledgement


__all__ = [
    "ControlEvent",
    "ReplicaAcknowledgement",
    "ReplicaAcknowledgment",
    "ReplicaRole",
    "ReplicationAcknowledgementPolicy",
    "ReplicationAcknowledgmentPolicy",
    "ReplicationDecision",
    "ReplicationMode",
    "ReplicationPolicy",
    "ReplicationReason",
    "ReplicationState",
    "event_digest_for",
]
