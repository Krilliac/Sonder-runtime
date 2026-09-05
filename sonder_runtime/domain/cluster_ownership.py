"""Scoped ownership epochs and lease fencing for cluster-managed state.

This module is a deterministic authority contract, not a consensus
implementation.  A deployment may use it behind a replicated store and an
established fencing provider.  Takeover therefore accepts only explicit
external proof that the previous owner was fenced and that acknowledged data
exists on at least two replicas.  An owner preference, a lease timeout, or a
third witness by itself never grants authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import time
from typing import Callable


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RESOURCE_KINDS = frozenset({"session", "attempt", "job", "approval", "memory_write"})
MAX_LEASE_SECONDS = 900
MAX_REPLICAS_IN_PROOF = 64


class OwnershipError(ValueError):
    """Invalid ownership data or a malformed lease request."""


class OwnershipConflict(OwnershipError):
    """A request conflicts with the current scoped owner or lease."""


class TakeoverDenied(OwnershipError):
    """Takeover lacks a complete external fencing/data acknowledgement proof."""


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise OwnershipError(f"{field} must be a bounded stable identity")
    return value


def _resource_kind(value: object) -> str:
    if not isinstance(value, str) or value not in _RESOURCE_KINDS:
        raise OwnershipError("resource_kind is not supported")
    return value


def _epoch(value: object, field: str) -> int:
    if type(value) is not int or not 1 <= value <= (1 << 63) - 1:
        raise OwnershipError(f"{field} must be a positive bounded integer")
    return value


def _lease_seconds(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LEASE_SECONDS:
        raise OwnershipError(
            f"lease_seconds must be an integer within 1..{MAX_LEASE_SECONDS}"
        )
    return value


@dataclass(frozen=True, slots=True)
class OwnerLease:
    """Capability bound to one resource key, owner, epoch, and expiry."""

    cluster_id: str
    resource_kind: str
    resource_id: str
    owner_id: str
    epoch: int
    token: str
    expires_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "resource_kind", _resource_kind(self.resource_kind))
        object.__setattr__(self, "resource_id", _identity(self.resource_id, "resource_id"))
        object.__setattr__(self, "owner_id", _identity(self.owner_id, "owner_id"))
        object.__setattr__(self, "epoch", _epoch(self.epoch, "epoch"))
        object.__setattr__(self, "token", _identity(self.token, "lease token"))
        if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, (int, float)):
            raise OwnershipError("expires_at must be numeric")
        if self.expires_at != self.expires_at or self.expires_at in (float("inf"), float("-inf")):
            raise OwnershipError("expires_at must be finite")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.resource_kind, self.resource_id, self.cluster_id


@dataclass(frozen=True, slots=True)
class OwnershipDecision:
    """Low-cardinality result of checking a lease at an authority boundary."""

    allowed: bool
    reason: str
    epoch: int | None = None


@dataclass(frozen=True, slots=True)
class TakeoverProof:
    """External evidence required before an owner epoch can advance."""

    cluster_id: str
    resource_kind: str
    resource_id: str
    previous_owner_id: str
    previous_epoch: int
    fence_receipt: str
    data_ack_epoch: int
    replica_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_id", _identity(self.cluster_id, "cluster_id"))
        object.__setattr__(self, "resource_kind", _resource_kind(self.resource_kind))
        object.__setattr__(self, "resource_id", _identity(self.resource_id, "resource_id"))
        object.__setattr__(self, "previous_owner_id", _identity(self.previous_owner_id, "previous_owner_id"))
        object.__setattr__(self, "previous_epoch", _epoch(self.previous_epoch, "previous_epoch"))
        if not isinstance(self.fence_receipt, str) or len(self.fence_receipt.encode("utf-8")) > 4096:
            raise OwnershipError("fence_receipt must be bounded text")
        if type(self.data_ack_epoch) is not int or not 0 <= self.data_ack_epoch <= (1 << 63) - 1:
            raise OwnershipError("data_ack_epoch must be a bounded integer")
        if type(self.replica_ids) is not tuple or not 1 <= len(self.replica_ids) <= MAX_REPLICAS_IN_PROOF:
            raise OwnershipError("replica_ids must be a bounded tuple")
        normalized = tuple(_identity(value, "replica_id") for value in self.replica_ids)
        if len(set(normalized)) != len(normalized):
            raise OwnershipError("replica_ids must not contain duplicates")
        object.__setattr__(self, "replica_ids", normalized)


class ClusterOwnershipAuthority:
    """Single-authority reference implementation with strict epoch fencing.

    The state is intentionally process-local.  Production cluster adapters
    must put the same transitions behind durable replication and an external
    fencing/consensus system; this class provides the contract and fail-closed
    semantics those adapters must preserve.
    """

    def __init__(
        self,
        cluster_id: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_resources: int = 10_000,
    ) -> None:
        self.cluster_id = _identity(cluster_id, "cluster_id")
        if not callable(clock):
            raise OwnershipError("clock must be callable")
        if type(max_resources) is not int or not 1 <= max_resources <= 1_000_000:
            raise OwnershipError("max_resources must be a bounded positive integer")
        self._clock = clock
        self._max_resources = max_resources
        self._leases: dict[tuple[str, str, str], OwnerLease] = {}
        # Retain the last epoch after release so a later owner can never reuse
        # a capability epoch for the same resource key.
        self._epochs: dict[tuple[str, str, str], int] = {}

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OwnershipError("clock returned a non-numeric value")
        if value != value or value in (float("inf"), float("-inf")):
            raise OwnershipError("clock returned a non-finite value")
        return float(value)

    def _make_lease(self, kind: str, resource_id: str, owner_id: str, epoch: int, ttl: int, now: float) -> OwnerLease:
        if len(self._leases) >= self._max_resources and (kind, resource_id, self.cluster_id) not in self._leases:
            raise OwnershipConflict("ownership resource capacity exhausted")
        return OwnerLease(
            self.cluster_id, kind, resource_id, owner_id, epoch,
            secrets.token_urlsafe(24), now + ttl,
        )

    def acquire(self, resource_kind: str, resource_id: str, owner_id: str, *, lease_seconds: int = 30) -> OwnerLease:
        kind = _resource_kind(resource_kind)
        resource = _identity(resource_id, "resource_id")
        owner = _identity(owner_id, "owner_id")
        ttl = _lease_seconds(lease_seconds)
        now = self._now()
        key = (kind, resource, self.cluster_id)
        current = self._leases.get(key)
        if current is not None and now < current.expires_at:
            raise OwnershipConflict("resource is already owned")
        epoch = self._epochs.get(key, 0) + 1
        lease = self._make_lease(kind, resource, owner, epoch, ttl, now)
        self._leases[key] = lease
        self._epochs[key] = epoch
        return lease

    def validate(self, lease: OwnerLease) -> OwnershipDecision:
        if not isinstance(lease, OwnerLease):
            return OwnershipDecision(False, "invalid_lease")
        if lease.cluster_id != self.cluster_id:
            return OwnershipDecision(False, "cluster_mismatch")
        current = self._leases.get(lease.key)
        if current is None:
            return OwnershipDecision(False, "no_owner")
        if current.epoch != lease.epoch:
            return OwnershipDecision(False, "stale_epoch", current.epoch)
        if current.owner_id != lease.owner_id:
            return OwnershipDecision(False, "owner_mismatch", current.epoch)
        if current.token != lease.token:
            return OwnershipDecision(False, "stale_lease", current.epoch)
        if self._now() >= current.expires_at:
            return OwnershipDecision(False, "lease_expired", current.epoch)
        return OwnershipDecision(True, "current", current.epoch)

    def renew(self, lease: OwnerLease, *, lease_seconds: int = 30) -> OwnerLease:
        ttl = _lease_seconds(lease_seconds)
        decision = self.validate(lease)
        if not decision.allowed:
            raise OwnershipConflict(decision.reason)
        now = self._now()
        renewed = OwnerLease(
            lease.cluster_id, lease.resource_kind, lease.resource_id,
            lease.owner_id, lease.epoch, lease.token, now + ttl,
        )
        self._leases[lease.key] = renewed
        return renewed

    def release(self, lease: OwnerLease) -> bool:
        if not isinstance(lease, OwnerLease) or lease.cluster_id != self.cluster_id:
            return False
        current = self._leases.get(lease.key)
        if current != lease:
            return False
        del self._leases[lease.key]
        return True

    def takeover(
        self,
        proof: TakeoverProof,
        *,
        new_owner_id: str,
        lease_seconds: int = 30,
        resource_id: str | None = None,
    ) -> OwnerLease:
        if not isinstance(proof, TakeoverProof):
            raise TakeoverDenied("takeover proof is invalid")
        owner = _identity(new_owner_id, "new_owner_id")
        ttl = _lease_seconds(lease_seconds)
        if proof.cluster_id != self.cluster_id:
            raise TakeoverDenied("takeover proof cluster does not match authority")
        if resource_id is not None and _identity(resource_id, "resource_id") != proof.resource_id:
            raise TakeoverDenied("takeover resource does not match proof")
        current = self._leases.get((proof.resource_kind, proof.resource_id, self.cluster_id))
        if current is None or current.owner_id != proof.previous_owner_id or current.epoch != proof.previous_epoch:
            raise TakeoverDenied("takeover current owner does not match proof")
        if owner == current.owner_id:
            raise TakeoverDenied("takeover owner must change")
        if not proof.fence_receipt.strip():
            raise TakeoverDenied("takeover requires external owner fence receipt")
        if len(proof.replica_ids) < 2:
            raise TakeoverDenied("takeover requires acknowledged data on at least two replicas")
        if proof.data_ack_epoch < current.epoch:
            raise TakeoverDenied("takeover acknowledged data is behind current epoch")
        now = self._now()
        replacement = self._make_lease(
            proof.resource_kind, proof.resource_id, owner,
            current.epoch + 1, ttl, now,
        )
        self._leases[replacement.key] = replacement
        self._epochs[replacement.key] = replacement.epoch
        return replacement


__all__ = [
    "ClusterOwnershipAuthority",
    "MAX_LEASE_SECONDS",
    "OwnerLease",
    "OwnershipConflict",
    "OwnershipDecision",
    "OwnershipError",
    "TakeoverDenied",
    "TakeoverProof",
]
