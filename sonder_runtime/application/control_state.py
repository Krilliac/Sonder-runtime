"""Application composition for an explicitly configured control-state provider.

The coordinator owns the order of one bounded append/read/fence operation and
the pure takeover gate.  It never discovers a provider, retries an ambiguous
write, elects an owner, or mutates a lease.  A caller must take the returned
proof to a separately owned authority before promotion.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.cluster_availability import (
    ControlStateEvent,
    FenceReceipt,
    OwnershipScope,
    ReplicatedControlStateCapabilities,
    ReplicationAcknowledgement,
    TakeoverDecision,
    evaluate_takeover,
    validate_replication_acknowledgement,
)
from ..domain.common.errors import DependencyUnavailable


_MAX_READ_LIMIT = 128
_MAX_CURSOR = (1 << 63) - 1
_MAX_REPLICAS = 64


def _cursor(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_CURSOR:
        raise ValueError("after_sequence must be a bounded non-negative integer")
    return value


def _limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_READ_LIMIT:
        raise ValueError(f"limit must be within 1..{_MAX_READ_LIMIT}")
    return value


@dataclass(frozen=True, slots=True)
class ControlStateTakeoverAttempt:
    """Receipts and the pure decision for one requested owner transition."""

    ownership: OwnershipScope
    new_owner_id: str
    event: ControlStateEvent
    acknowledgement: ReplicationAcknowledgement
    fence_receipt: FenceReceipt
    decision: TakeoverDecision

    def __post_init__(self) -> None:
        if not isinstance(self.ownership, OwnershipScope):
            raise TypeError("ownership must be an OwnershipScope")
        if not isinstance(self.new_owner_id, str) or not self.new_owner_id:
            raise ValueError("new_owner_id is required")
        # Reuse the domain identity grammar without exposing another parser.
        OwnershipScope(
            self.ownership.cluster_id,
            self.ownership.resource_kind,
            self.ownership.resource_id,
            self.new_owner_id,
            self.ownership.epoch,
        )
        if not isinstance(self.event, ControlStateEvent):
            raise TypeError("event must be a ControlStateEvent")
        if self.event.scope != self.ownership:
            raise ValueError("event scope does not match ownership")
        if not isinstance(self.acknowledgement, ReplicationAcknowledgement):
            raise TypeError("acknowledgement must be a ReplicationAcknowledgement")
        if not isinstance(self.fence_receipt, FenceReceipt):
            raise TypeError("fence_receipt must be a FenceReceipt")
        if not isinstance(self.decision, TakeoverDecision):
            raise TypeError("decision must be a TakeoverDecision")

    def as_dict(self) -> dict[str, object]:
        """Return bounded, content-free evidence for an operator record."""
        return {
            "ownership": self.ownership.as_dict(),
            "new_owner_id": self.new_owner_id,
            "event": self.event.as_dict(),
            "acknowledgement": self.acknowledgement.as_dict(),
            "fence_receipt": self.fence_receipt.as_dict(),
            "decision": {
                "allowed": self.decision.allowed,
                "reason": self.decision.reason,
                "next_epoch": self.decision.next_epoch,
                "data_replica_count": self.decision.data_replica_count,
            },
        }


class ExternalControlStateCoordinator:
    """Compose provider receipts with Sonder's fail-closed takeover gate.

    ``provider`` is intentionally an injected object.  The coordinator does
    not create a network client from configuration and does not turn a
    capability declaration into evidence.  ``prepare_takeover`` returns a
    decision only; promotion remains the responsibility of an external,
    durable ownership authority.
    """

    def __init__(
        self,
        provider: Any,
        capabilities: ReplicatedControlStateCapabilities,
        *,
        minimum_data_replicas: int = 2,
    ) -> None:
        if not callable(getattr(provider, "append", None)):
            raise TypeError("provider must provide append(event)")
        if not callable(getattr(provider, "read", None)):
            raise TypeError("provider must provide read(cluster_id, ...)")
        if not callable(getattr(provider, "fence", None)):
            raise TypeError("provider must provide fence(ownership)")
        if not isinstance(capabilities, ReplicatedControlStateCapabilities):
            raise TypeError("provider capabilities are required")
        protocol_version = getattr(provider, "protocol_version", None)
        if type(protocol_version) is not int:
            raise ValueError("provider protocol_version is required")
        if protocol_version != capabilities.protocol_version:
            raise ValueError("provider protocol version does not match capabilities")
        provider_id = getattr(provider, "provider_id", None)
        if provider_id is not None and provider_id != capabilities.provider_id:
            raise ValueError("provider identity does not match capabilities")
        if (
            type(minimum_data_replicas) is not int
            or not 2 <= minimum_data_replicas <= _MAX_REPLICAS
            or minimum_data_replicas > capabilities.data_replica_count
        ):
            raise ValueError(
                "minimum_data_replicas must be within 2..64 and fit the provider"
            )
        self.provider = provider
        self.capabilities = capabilities
        self.minimum_data_replicas = minimum_data_replicas

    def _validate_ack(
        self,
        event: ControlStateEvent,
        acknowledgement: object,
    ) -> ReplicationAcknowledgement:
        if not isinstance(acknowledgement, ReplicationAcknowledgement):
            raise DependencyUnavailable("control-state provider returned an invalid acknowledgement")
        decision = validate_replication_acknowledgement(
            event,
            acknowledgement,
            self.capabilities,
            minimum_data_replicas=self.minimum_data_replicas,
        )
        if not decision.accepted:
            raise DependencyUnavailable(
                f"control-state acknowledgement rejected: {decision.reason}"
            )
        return acknowledgement

    def append(self, event: ControlStateEvent) -> ReplicationAcknowledgement:
        """Append one event and require its exact durable replica evidence."""
        if not isinstance(event, ControlStateEvent):
            raise TypeError("event must be a ControlStateEvent")
        if event.protocol_version != self.capabilities.protocol_version:
            raise DependencyUnavailable("control-state event protocol does not match provider")
        try:
            acknowledgement = self.provider.append(event)
        except DependencyUnavailable:
            raise
        except Exception as exc:
            raise DependencyUnavailable(
                f"control-state provider append failed: {type(exc).__name__}"
            ) from exc
        return self._validate_ack(event, acknowledgement)

    def read(
        self,
        cluster_id: str,
        *,
        after_sequence: int = 0,
        limit: int = _MAX_READ_LIMIT,
    ) -> tuple[ControlStateEvent, ...]:
        """Read one bounded page and enforce cluster/protocol/order identity."""
        if not isinstance(cluster_id, str) or not cluster_id:
            raise ValueError("cluster_id is required")
        _cursor(after_sequence)
        _limit(limit)
        try:
            events = self.provider.read(
                cluster_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except DependencyUnavailable:
            raise
        except Exception as exc:
            raise DependencyUnavailable(
                f"control-state provider read failed: {type(exc).__name__}"
            ) from exc
        if not isinstance(events, tuple) or len(events) > limit:
            raise DependencyUnavailable("control-state provider returned an invalid event page")
        result: list[ControlStateEvent] = []
        previous = after_sequence
        for event in events:
            if not isinstance(event, ControlStateEvent):
                raise DependencyUnavailable("control-state provider returned an invalid event")
            if (
                event.cluster_id != cluster_id
                or event.protocol_version != self.capabilities.protocol_version
                or event.sequence <= previous
            ):
                raise DependencyUnavailable("control-state provider event page is not ordered")
            result.append(event)
            previous = event.sequence
        return tuple(result)

    def prepare_takeover(
        self,
        ownership: OwnershipScope,
        event: ControlStateEvent,
        *,
        new_owner_id: str,
        acknowledgement: ReplicationAcknowledgement | None = None,
    ) -> ControlStateTakeoverAttempt:
        """Collect receipts and evaluate takeover without promoting an owner."""
        if not isinstance(ownership, OwnershipScope):
            raise TypeError("ownership must be an OwnershipScope")
        if not isinstance(event, ControlStateEvent):
            raise TypeError("event must be a ControlStateEvent")
        if event.scope != ownership:
            raise ValueError("event scope does not match ownership")
        if not isinstance(new_owner_id, str) or not new_owner_id:
            raise ValueError("new_owner_id is required")
        # Validate the identity and preserve the current epoch for the gate.
        OwnershipScope(
            ownership.cluster_id,
            ownership.resource_kind,
            ownership.resource_id,
            new_owner_id,
            ownership.epoch,
        )
        ack = (
            self.append(event)
            if acknowledgement is None
            else self._validate_ack(event, acknowledgement)
        )
        try:
            fence = self.provider.fence(ownership)
        except DependencyUnavailable:
            raise
        except Exception as exc:
            raise DependencyUnavailable(
                f"control-state provider fence failed: {type(exc).__name__}"
            ) from exc
        if not isinstance(fence, FenceReceipt):
            raise DependencyUnavailable("control-state provider returned an invalid fence receipt")
        decision = evaluate_takeover(
            ownership,
            new_owner_id=new_owner_id,
            event=event,
            acknowledgement=ack,
            fence_receipt=fence,
            provider=self.capabilities,
            minimum_data_replicas=self.minimum_data_replicas,
        )
        return ControlStateTakeoverAttempt(
            ownership=ownership,
            new_owner_id=new_owner_id,
            event=event,
            acknowledgement=ack,
            fence_receipt=fence,
            decision=decision,
        )


__all__ = ["ControlStateTakeoverAttempt", "ExternalControlStateCoordinator"]
