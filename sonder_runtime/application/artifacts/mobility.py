"""Durable intent and one explicit bounded mobility-v1 dispatch service.

The application service composes only abstract source, fixed-peer, and journal
ports.  It has no filesystem, route, CLI, scheduler, timer, or worker
dependency: immutable intent is written first, and each invocation retains a
real local dispatch lock plus a fenced lease until its one outcome transition.

The journal's records are private implementation data.  ``public_status`` is
the only projection intended for a CLI, REPL, log, or status surface.  It
omits destination transport material, credential generation, source scope,
lease token, receipt capability, and receiver receipt details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from types import MappingProxyType
from typing import Mapping, Protocol

from .mobility_source import MobilitySourceError, SourceArtifactRange
from .transfer import (
    MOBILITY_V1_VERSION,
    TransferError,
    recipient_mobility_attestation,
)

_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[!-~]{1,128}")
_COMMAND = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_PROTECTED_CAPABILITY = re.compile(r"v1\.[0-9a-f]{24}\.[0-9a-f]{96}")
_CAPABILITY = re.compile(r"[0-9a-f]{64}")
_ATTESTATION_PATH = "/v1/artifact-transfers/recipient-attestation"

MAX_RECEIPT_TTL_SECONDS = 31 * 24 * 60 * 60
MAX_ATTEMPTS = 256
MAX_TIMESTAMP = 2**63 - 1
MAX_OBJECT_BYTES = 64 * 1024**3
MAX_CHUNK_BYTES = 1024 * 1024

MOBILITY_STATES = frozenset(
    {
        "ready",
        "dispatching",
        "resumable",
        "awaiting_seal",
        "retryable_blocked",
        "sealed",
        "terminal_blocked",
        "expired",
    }
)
TERMINAL_STATES = frozenset({"sealed", "terminal_blocked", "expired"})
DISPATCH_ELIGIBLE_STATES = frozenset(
    {"ready", "resumable", "awaiting_seal", "retryable_blocked"}
)
LEASE_TRANSITIONS = frozenset(
    {
        "resumable",
        "awaiting_seal",
        "retryable_blocked",
        "sealed",
        "terminal_blocked",
        "expired",
    }
)
_RECEIPT_STATES = frozenset({"open", "verifying", "sealed"})
_OUTCOME_CODES = frozenset(
    {
        "",
        "IMMUTABLE_FENCE",
        "ATTEMPT_LIMIT",
        "SOURCE_UNAVAILABLE",
        "MOBILITY_CAPACITY",
        "MOBILITY_QUOTA",
        "MOBILITY_BUSY",
        "MOBILITY_UNAVAILABLE",
        "MOBILITY_FORBIDDEN",
        "MOBILITY_INTEGRITY",
        "MOBILITY_PROTOCOL",
        "MOBILITY_RECEIPT_EXPIRED",
    }
)


class MobilityJournalError(RuntimeError):
    """A stable local journal code with no paths, credentials, or peer text."""


class MobilityPeerAvailabilityError(TransferError):
    """A definitive, received peer availability response with no lost reply."""


class _ArtifactMobilityPeerRequestFences:
    """Private one-shot lease fence consumed inside a fixed peer transport.

    The dispatch service is the only production issuer.  A peer receives it
    through an internal scope, never as a public peer-method argument, and can
    only consume an exact precomputed method/path pair before its own request.
    """

    __slots__ = ()

    def before_request(self, method: str, path: str) -> None:
        raise NotImplementedError


def _fail(code: str) -> None:
    raise MobilityJournalError(code)


def _identifier(value: object, *, code: str = "INVALID_REQUEST") -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        _fail(code)
    return value


def _operation_id(value: object, *, code: str = "INVALID_REQUEST") -> str:
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        _fail(code)
    return value


def _digest(value: object, *, code: str = "INVALID_REQUEST") -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail(code)
    return value


def _timestamp(value: object, *, code: str = "INVALID_REQUEST") -> float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= MAX_TIMESTAMP
    ):
        _fail(code)
    return float(value)


def _immutable_spec(
    value: object, *, code: str = "INVALID_REQUEST"
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "sha256",
        "size_bytes",
        "media_type",
    }:
        _fail(code)
    digest = value.get("sha256")
    size = value.get("size_bytes")
    media_type = value.get("media_type")
    if (
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or type(size) is not int
        or not 0 <= size <= MAX_OBJECT_BYTES
        or not isinstance(media_type, str)
        or not 1 <= len(media_type) <= 128
        or any(ord(character) < 32 or ord(character) > 126 for character in media_type)
    ):
        _fail(code)
    return MappingProxyType(
        {"sha256": digest, "size_bytes": size, "media_type": media_type}
    )


def _canonical_spec(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
    except (TypeError, ValueError, UnicodeError):
        _fail("INTEGRITY")


def derive_remote_command(destination_scope_id: object, operation_id: object) -> str:
    """Derive the one canonical receiver command for an immutable operation."""
    scope = _digest(destination_scope_id)
    identity = _operation_id(operation_id)
    command = f"{MOBILITY_V1_VERSION}.{scope[:16]}.{identity}"
    if _COMMAND.fullmatch(command) is None:
        _fail("INTEGRITY")
    return command


@dataclass(frozen=True, repr=False)
class MobilityDispatchContext:
    """Current trusted local source and fixed-destination binding facts."""

    source_owner_id: str
    source_scope_id: str
    destination_label: str
    destination_scope_id: str
    credential_generation: str
    destination_binding_hmac: str = field(repr=False)
    credential_material: object = field(repr=False, compare=False)
    attempt_lease_seconds: int = 30
    receipt_ttl_seconds: int = 7 * 24 * 60 * 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_owner_id", _identifier(self.source_owner_id))
        object.__setattr__(self, "source_scope_id", _digest(self.source_scope_id))
        object.__setattr__(
            self, "destination_label", _identifier(self.destination_label)
        )
        object.__setattr__(
            self, "destination_scope_id", _digest(self.destination_scope_id)
        )
        object.__setattr__(
            self, "credential_generation", _digest(self.credential_generation)
        )
        object.__setattr__(
            self, "destination_binding_hmac", _digest(self.destination_binding_hmac)
        )
        if self.credential_material is None:
            _fail("INVALID_REQUEST")
        if (
            type(self.attempt_lease_seconds) is not int
            or not 2 <= self.attempt_lease_seconds <= 3600
            or type(self.receipt_ttl_seconds) is not int
            or not 60 <= self.receipt_ttl_seconds <= MAX_RECEIPT_TTL_SECONDS
        ):
            _fail("INVALID_REQUEST")

    def __repr__(self) -> str:
        return (
            "MobilityDispatchContext("
            f"destination_label={self.destination_label!r}, private=True)"
        )


class ReceiptCapabilityProtector(Protocol):
    """Adapter-owned encryption seam for one private journal capability.

    The application generates the opaque 256-bit capability and operation ID,
    while the persistence adapter derives an AEAD key from current trusted peer
    credential material and stores only the encrypted envelope. This keeps key
    handling and the cryptographic dependency outside the application layer.
    """

    def protect_receipt_capability(
        self, capability: str, credential_material: object, operation_id: str
    ) -> str: ...

    def recover_receipt_capability(
        self, protected: str, credential_material: object, operation_id: str
    ) -> str: ...


@dataclass(frozen=True)
class MobilityOperationRequest:
    """Caller-supplied immutable inputs, deliberately excluding ID and command."""

    source_owner_id: str
    source_scope_id: str
    source_artifact_id: str
    immutable_spec: Mapping[str, object]
    destination_label: str
    destination_scope_id: str
    credential_generation: str
    destination_binding_hmac: str = field(repr=False)
    receipt_ttl_seconds: int = 7 * 24 * 60 * 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_owner_id", _identifier(self.source_owner_id))
        object.__setattr__(self, "source_scope_id", _digest(self.source_scope_id))
        object.__setattr__(
            self, "source_artifact_id", _operation_id(self.source_artifact_id)
        )
        object.__setattr__(self, "immutable_spec", _immutable_spec(self.immutable_spec))
        object.__setattr__(
            self, "destination_label", _identifier(self.destination_label)
        )
        object.__setattr__(
            self, "destination_scope_id", _digest(self.destination_scope_id)
        )
        object.__setattr__(
            self, "credential_generation", _digest(self.credential_generation)
        )
        object.__setattr__(
            self, "destination_binding_hmac", _digest(self.destination_binding_hmac)
        )
        if (
            type(self.receipt_ttl_seconds) is not int
            or not 60 <= self.receipt_ttl_seconds <= MAX_RECEIPT_TTL_SECONDS
        ):
            _fail("INVALID_REQUEST")

    def __repr__(self) -> str:
        return (
            "MobilityOperationRequest("
            f"source_artifact_id={self.source_artifact_id!r}, "
            f"destination_label={self.destination_label!r})"
        )


@dataclass(frozen=True)
class MobilityImmutableFence:
    """Current local facts that must exactly match an operation before dispatch."""

    source_owner_id: str
    source_scope_id: str
    source_artifact_id: str
    immutable_spec: Mapping[str, object]
    destination_scope_id: str
    credential_generation: str
    destination_binding_hmac: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_owner_id", _identifier(self.source_owner_id))
        object.__setattr__(self, "source_scope_id", _digest(self.source_scope_id))
        object.__setattr__(
            self, "source_artifact_id", _operation_id(self.source_artifact_id)
        )
        object.__setattr__(self, "immutable_spec", _immutable_spec(self.immutable_spec))
        object.__setattr__(
            self, "destination_scope_id", _digest(self.destination_scope_id)
        )
        object.__setattr__(
            self, "credential_generation", _digest(self.credential_generation)
        )
        object.__setattr__(
            self, "destination_binding_hmac", _digest(self.destination_binding_hmac)
        )

    @classmethod
    def from_request(
        cls, request: MobilityOperationRequest
    ) -> "MobilityImmutableFence":
        if not isinstance(request, MobilityOperationRequest):
            _fail("INVALID_REQUEST")
        return cls(
            source_owner_id=request.source_owner_id,
            source_scope_id=request.source_scope_id,
            source_artifact_id=request.source_artifact_id,
            immutable_spec=request.immutable_spec,
            destination_scope_id=request.destination_scope_id,
            credential_generation=request.credential_generation,
            destination_binding_hmac=request.destination_binding_hmac,
        )

    def __repr__(self) -> str:
        return "MobilityImmutableFence(private=True)"


@dataclass(frozen=True)
class ReceiptCheckpoint:
    """The bounded receiver facts that are safe to retain for an explicit resume."""

    transfer_id: str
    artifact_id: str | None
    state: str
    offset: int
    chunk_bytes: int
    revision: int
    expires_at: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transfer_id", _operation_id(self.transfer_id, code="INVALID_RECEIPT")
        )
        if self.artifact_id is not None:
            object.__setattr__(
                self,
                "artifact_id",
                _operation_id(self.artifact_id, code="INVALID_RECEIPT"),
            )
        if self.state not in _RECEIPT_STATES:
            _fail("INVALID_RECEIPT")
        if (self.state == "sealed") != (self.artifact_id is not None):
            _fail("INVALID_RECEIPT")
        if type(self.offset) is not int or self.offset < 0:
            _fail("INVALID_RECEIPT")
        if (
            type(self.chunk_bytes) is not int
            or not 65536 <= self.chunk_bytes <= MAX_CHUNK_BYTES
        ):
            _fail("INVALID_RECEIPT")
        if type(self.revision) is not int or not 1 <= self.revision <= MAX_TIMESTAMP:
            _fail("INVALID_RECEIPT")
        object.__setattr__(
            self, "expires_at", _timestamp(self.expires_at, code="INVALID_RECEIPT")
        )

    def __repr__(self) -> str:
        return (
            "ReceiptCheckpoint("
            f"state={self.state!r}, offset={self.offset!r}, expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, repr=False)
class DispatchLease:
    """One opaque, attempt-epoch-scoped local dispatch lease."""

    operation_id: str
    source_owner_id: str
    epoch: int
    token: str = field(repr=False)
    expires_at: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_id", _operation_id(self.operation_id, code="INTEGRITY")
        )
        object.__setattr__(
            self, "source_owner_id", _identifier(self.source_owner_id, code="INTEGRITY")
        )
        if type(self.epoch) is not int or not 1 <= self.epoch <= MAX_ATTEMPTS:
            _fail("INTEGRITY")
        object.__setattr__(self, "token", _digest(self.token, code="INTEGRITY"))
        object.__setattr__(
            self, "expires_at", _timestamp(self.expires_at, code="INTEGRITY")
        )

    def __repr__(self) -> str:
        return f"DispatchLease(epoch={self.epoch!r}, expires_at={self.expires_at!r})"


@dataclass(frozen=True, repr=False)
class MobilityOperation:
    """Private durable operation record.  Use :meth:`public_status` externally."""

    operation_id: str
    source_owner_id: str
    source_scope_id: str
    source_artifact_id: str
    immutable_spec: Mapping[str, object]
    destination_label: str
    destination_scope_id: str
    remote_command_id: str
    credential_generation: str
    destination_binding_hmac: str
    protected_receipt_capability: str
    state: str
    outcome_code: str
    attempt_epoch: int
    lease_token: str | None
    lease_expires_at: float | None
    created_at: float
    updated_at: float
    receipt_expires_at: float
    receipt: ReceiptCheckpoint | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_id", _operation_id(self.operation_id, code="INTEGRITY")
        )
        object.__setattr__(
            self, "source_owner_id", _identifier(self.source_owner_id, code="INTEGRITY")
        )
        object.__setattr__(
            self, "source_scope_id", _digest(self.source_scope_id, code="INTEGRITY")
        )
        object.__setattr__(
            self,
            "source_artifact_id",
            _operation_id(self.source_artifact_id, code="INTEGRITY"),
        )
        object.__setattr__(
            self,
            "immutable_spec",
            _immutable_spec(self.immutable_spec, code="INTEGRITY"),
        )
        object.__setattr__(
            self,
            "destination_label",
            _identifier(self.destination_label, code="INTEGRITY"),
        )
        object.__setattr__(
            self,
            "destination_scope_id",
            _digest(self.destination_scope_id, code="INTEGRITY"),
        )
        expected_command = derive_remote_command(
            self.destination_scope_id, self.operation_id
        )
        if self.remote_command_id != expected_command:
            _fail("INTEGRITY")
        object.__setattr__(
            self,
            "credential_generation",
            _digest(self.credential_generation, code="INTEGRITY"),
        )
        object.__setattr__(
            self,
            "destination_binding_hmac",
            _digest(self.destination_binding_hmac, code="INTEGRITY"),
        )
        if (
            not isinstance(self.protected_receipt_capability, str)
            or _PROTECTED_CAPABILITY.fullmatch(self.protected_receipt_capability)
            is None
        ):
            _fail("INTEGRITY")
        if self.state not in MOBILITY_STATES or self.outcome_code not in _OUTCOME_CODES:
            _fail("INTEGRITY")
        if (
            type(self.attempt_epoch) is not int
            or not 0 <= self.attempt_epoch <= MAX_ATTEMPTS
        ):
            _fail("INTEGRITY")
        if self.lease_token is None:
            if self.lease_expires_at is not None or self.state == "dispatching":
                _fail("INTEGRITY")
        else:
            object.__setattr__(
                self, "lease_token", _digest(self.lease_token, code="INTEGRITY")
            )
            object.__setattr__(
                self,
                "lease_expires_at",
                _timestamp(self.lease_expires_at, code="INTEGRITY"),
            )
            if self.state != "dispatching" or self.attempt_epoch < 1:
                _fail("INTEGRITY")
        object.__setattr__(
            self, "created_at", _timestamp(self.created_at, code="INTEGRITY")
        )
        object.__setattr__(
            self, "updated_at", _timestamp(self.updated_at, code="INTEGRITY")
        )
        object.__setattr__(
            self,
            "receipt_expires_at",
            _timestamp(self.receipt_expires_at, code="INTEGRITY"),
        )
        if self.updated_at < self.created_at:
            _fail("INTEGRITY")
        if self.receipt is not None:
            if not isinstance(self.receipt, ReceiptCheckpoint):
                _fail("INTEGRITY")
            if self.receipt.offset > self.immutable_spec["size_bytes"]:
                _fail("INTEGRITY")
        if self.state == "sealed" and (
            self.receipt is None or self.receipt.state != "sealed"
        ):
            _fail("INTEGRITY")

    def __repr__(self) -> str:
        return (
            "MobilityOperation("
            f"operation_id={self.operation_id!r}, destination_label={self.destination_label!r}, "
            f"state={self.state!r}, attempt_epoch={self.attempt_epoch!r})"
        )

    def public_status(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "source_artifact_id": self.source_artifact_id,
            "destination_label": self.destination_label,
            "state": self.state,
            "outcome_code": self.outcome_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ArtifactMobilityJournalRepository(Protocol):
    """Private persistence port.  It is not a public runtime controller."""

    def create_operation(self, operation: MobilityOperation) -> MobilityOperation: ...

    def load_operation(
        self, operation_id: str, source_owner_id: str
    ) -> MobilityOperation: ...

    def public_status(
        self, operation_id: str, source_owner_id: str
    ) -> dict[str, object]: ...

    def protect_receipt_capability(
        self, capability: str, credential_material: object, operation_id: str
    ) -> str: ...

    def recover_receipt_capability(
        self, protected: str, credential_material: object, operation_id: str
    ) -> str: ...


class ArtifactMobilityJournal:
    """Generate immutable local intent without contacting a peer.

    A future operator-invoked service may obtain the protected receipt capability
    through :meth:`receipt_capability_for` after it has acquired the operation
    lock and current lease.  This class starts no worker and has no retry loop.
    """

    def __init__(
        self,
        repository: ArtifactMobilityJournalRepository,
        *,
        clock=time.time,
        operation_id_factory=None,
        receipt_capability_factory=None,
        receipt_protector: ReceiptCapabilityProtector | None = None,
    ) -> None:
        if operation_id_factory is None:
            operation_id_factory = lambda: secrets.token_hex(16)
        if receipt_capability_factory is None:
            receipt_capability_factory = lambda: secrets.token_hex(32)
        if receipt_protector is None:
            receipt_protector = repository
        if (
            not callable(clock)
            or not callable(operation_id_factory)
            or not callable(receipt_capability_factory)
            or not callable(
                getattr(receipt_protector, "protect_receipt_capability", None)
            )
            or not callable(
                getattr(receipt_protector, "recover_receipt_capability", None)
            )
        ):
            _fail("INVALID_REQUEST")
        self._repository = repository
        self._clock = clock
        self._operation_id_factory = operation_id_factory
        self._receipt_capability_factory = receipt_capability_factory
        self._receipt_protector = receipt_protector

    def create_operation(
        self, request: MobilityOperationRequest, *, credential_material: object
    ) -> MobilityOperation:
        """Persist intent and protected receipt material before any peer action."""
        if not isinstance(request, MobilityOperationRequest):
            _fail("INVALID_REQUEST")
        now = _timestamp(self._clock(), code="UNAVAILABLE")
        receipt_expiry = _timestamp(
            now + request.receipt_ttl_seconds, code="UNAVAILABLE"
        )
        # A random collision is fantastically unlikely, but the tombstone rule
        # means it must be retried rather than reused if it occurs.
        for _attempt in range(8):
            operation_id = _operation_id(
                self._operation_id_factory(), code="UNAVAILABLE"
            )
            receipt_capability = self._receipt_capability_factory()
            if (
                not isinstance(receipt_capability, str)
                or _CAPABILITY.fullmatch(receipt_capability) is None
            ):
                _fail("UNAVAILABLE")
            protected = self._receipt_protector.protect_receipt_capability(
                receipt_capability, credential_material, operation_id
            )
            operation = MobilityOperation(
                operation_id=operation_id,
                source_owner_id=request.source_owner_id,
                source_scope_id=request.source_scope_id,
                source_artifact_id=request.source_artifact_id,
                immutable_spec=request.immutable_spec,
                destination_label=request.destination_label,
                destination_scope_id=request.destination_scope_id,
                remote_command_id=derive_remote_command(
                    request.destination_scope_id, operation_id
                ),
                credential_generation=request.credential_generation,
                destination_binding_hmac=request.destination_binding_hmac,
                protected_receipt_capability=protected,
                state="ready",
                outcome_code="",
                attempt_epoch=0,
                lease_token=None,
                lease_expires_at=None,
                created_at=now,
                updated_at=now,
                receipt_expires_at=receipt_expiry,
                receipt=None,
            )
            try:
                return self._repository.create_operation(operation)
            except MobilityJournalError as error:
                if error.args == ("NO_REUSE",):
                    continue
                raise
        _fail("ID_EXHAUSTED")

    def receipt_capability_for(
        self, operation: MobilityOperation, *, credential_material: object
    ) -> str:
        """Recover the generated capability for trusted later dispatch composition."""
        if not isinstance(operation, MobilityOperation):
            _fail("INVALID_REQUEST")
        return self._receipt_protector.recover_receipt_capability(
            operation.protected_receipt_capability,
            credential_material,
            operation.operation_id,
        )


class _AttemptRequestFences(_ArtifactMobilityPeerRequestFences):
    """Consume one service-issued lease proof for each fixed peer request.

    The service creates this object only after source, destination, and
    immutable-fence validation.  A peer gets no mutable target or callback:
    it can consume the next precomputed request pair exactly once.  Closing
    clears its internal references so a retained object cannot prove a later
    request after the enclosing peer call has returned.
    """

    __slots__ = (
        "_repository",
        "_lease",
        "_lock",
        "_now",
        "_lease_seconds",
        "_targets",
        "_index",
        "_active",
    )

    def __init__(
        self,
        *,
        repository,
        lease: DispatchLease,
        lock,
        now,
        lease_seconds: int,
        targets: tuple[tuple[str, str], ...],
    ) -> None:
        if (
            type(targets) is not tuple
            or not targets
            or any(
                type(target) is not tuple
                or len(target) != 2
                or type(target[0]) is not str
                or type(target[1]) is not str
                or target[0] not in {"GET", "POST", "PUT"}
                or not target[1].startswith("/")
                for target in targets
            )
        ):
            raise TransferError("MOBILITY_INTEGRITY")
        self._repository = repository
        self._lease = lease
        self._lock = lock
        self._now = now
        self._lease_seconds = lease_seconds
        self._targets = targets
        self._index = 0
        self._active = True

    def close(self) -> None:
        """Make this single peer-call capability permanently unusable."""
        self._active = False
        self._repository = None
        self._lease = None
        self._lock = None
        self._now = None
        self._lease_seconds = 0
        self._targets = ()
        self._index = 0

    def before_request(self, method: str, path: str) -> None:
        """Renew the held lease for the next exact pinned transport request."""
        if (
            not self._active
            or type(method) is not str
            or type(path) is not str
            or self._index >= len(self._targets)
            or (method, path) != self._targets[self._index]
        ):
            self.close()
            raise TransferError("MOBILITY_INTEGRITY")
        assertion_now = self._now()
        self._repository.assert_current_lease(
            self._lease, lock=self._lock, now=assertion_now
        )
        # The assertion may have blocked on SQLite.  Renew with a new clock
        # sample so a lease that expired while it ran cannot be revived using
        # the assertion's stale timestamp.
        renewal_now = self._now()
        renewed_lease = self._repository.renew_dispatch(
            self._lease,
            lock=self._lock,
            now=renewal_now,
            lease_seconds=self._lease_seconds,
        )
        # Renewal can also block.  Prove the returned lease is still live at
        # the only point the peer may proceed to its concrete transport call.
        transport_now = self._now()
        if renewed_lease.expires_at <= transport_now:
            self.close()
            _fail("LEASE_LOST")
        self._lease = renewed_lease
        self._index += 1

    def finish(self) -> DispatchLease:
        """Require the peer to have fenced every request it declared."""
        if not self._active or self._index != len(self._targets):
            self.close()
            raise TransferError("MOBILITY_INTEGRITY")
        lease = self._lease
        self.close()
        return lease


class ArtifactMobilityDispatchService:
    """Run one synchronous, explicitly invoked send or resume attempt.

    The service owns no thread, timer, retry policy, route, or source mutation.
    Every peer method is preceded by a fresh immutable-fence check and a lease
    renewal while the same nonblocking OS lock remains continuously held.
    """

    _ATTESTATION_FIELDS = frozenset(
        {
            "protocol_version",
            "receiver_identity_id",
            "principal_id",
            "project_id",
            "authorized_source_owner_id",
            "grant_id",
            "grant_revision",
            "can_write",
            "max_object_bytes",
            "sha256",
        }
    )
    _ENVELOPE_FIELDS = frozenset(
        {
            "protocol_version",
            "recipient_attestation",
            "command_id",
            "spec",
            "receipt",
        }
    )
    _RECEIPT_FIELDS = frozenset(
        {"transfer_id", "state", "offset", "chunk_bytes", "expires_at", "revision"}
    )
    _ACK_FIELDS = frozenset({"offset", "next_offset", "chunk_sha256", "revision"})

    def __init__(
        self,
        *,
        source_reader,
        peer,
        repository,
        journal: ArtifactMobilityJournal,
        current_context,
        clock=time.time,
    ) -> None:
        source_methods = ("inspect_sealed", "read_range")
        peer_methods = (
            "_request_fence_scope",
            "recipient_attestation",
            "begin",
            "inspect_receipt",
            "append",
            "seal",
        )
        repository_methods = (
            "load_operation_for_fencing",
            "load_operation",
            "try_acquire_dispatch_lock",
            "acquire_dispatch",
            "assert_current_lease",
            "renew_dispatch",
            "assert_immutable_fence",
            "transition_with_lease",
        )
        if (
            any(
                not callable(getattr(source_reader, name, None))
                for name in source_methods
            )
            or any(not callable(getattr(peer, name, None)) for name in peer_methods)
            or any(
                not callable(getattr(repository, name, None))
                for name in repository_methods
            )
            or not isinstance(journal, ArtifactMobilityJournal)
            or not callable(current_context)
            or not callable(clock)
        ):
            _fail("INVALID_REQUEST")
        self._source_reader = source_reader
        self._peer = peer
        self._repository = repository
        self._journal = journal
        self._current_context = current_context
        self._clock = clock

    def __repr__(self) -> str:
        return "ArtifactMobilityDispatchService(private=True)"

    def send(
        self, source_artifact_id: object, *, confirm_destination: object
    ) -> MobilityOperation:
        """Persist one fresh immutable intent, then run its bounded attempt."""
        context = self._context()
        if (
            not isinstance(confirm_destination, str)
            or not hmac.compare_digest(confirm_destination, context.destination_label)
        ):
            _fail("CONFIRMATION_REQUIRED")
        source_id = _operation_id(source_artifact_id)
        immutable_spec = self._inspect_source(source_id)
        request = MobilityOperationRequest(
            source_owner_id=context.source_owner_id,
            source_scope_id=context.source_scope_id,
            source_artifact_id=source_id,
            immutable_spec=immutable_spec,
            destination_label=context.destination_label,
            destination_scope_id=context.destination_scope_id,
            credential_generation=context.credential_generation,
            destination_binding_hmac=context.destination_binding_hmac,
            receipt_ttl_seconds=context.receipt_ttl_seconds,
        )
        operation = self._journal.create_operation(
            request, credential_material=context.credential_material
        )
        return self._attempt(operation)

    def resume(self, operation_id: object) -> MobilityOperation:
        """Run one fresh attempt for exactly one eligible nonterminal record."""
        identity = _operation_id(operation_id, code="NOT_FOUND")
        operation = self._repository.load_operation_for_fencing(identity)
        if operation.state in TERMINAL_STATES:
            _fail("TERMINAL")
        if operation.state not in DISPATCH_ELIGIBLE_STATES:
            _fail("BUSY")
        return self._attempt(operation)

    def _context(self) -> MobilityDispatchContext:
        try:
            current = self._current_context()
        except Exception:
            raise TransferError("MOBILITY_UNAVAILABLE") from None
        if not isinstance(current, MobilityDispatchContext):
            raise TransferError("MOBILITY_UNAVAILABLE")
        return current

    def _now(self) -> float:
        return _timestamp(self._clock(), code="UNAVAILABLE")

    def _inspect_source(self, source_artifact_id: str) -> dict[str, object]:
        try:
            record = self._source_reader.inspect_sealed(source_artifact_id)
        except MobilitySourceError:
            raise
        except Exception:
            raise MobilitySourceError("UNAVAILABLE") from None
        if (
            not isinstance(record, Mapping)
            or set(record) != {
                "source_artifact_id",
                "sha256",
                "size_bytes",
                "media_type",
            }
            or record.get("source_artifact_id") != source_artifact_id
        ):
            raise MobilitySourceError("INVALID_SPEC")
        try:
            return dict(
                _immutable_spec(
                    {
                        "sha256": record.get("sha256"),
                        "size_bytes": record.get("size_bytes"),
                        "media_type": record.get("media_type"),
                    },
                    code="INVALID_REQUEST",
                )
            )
        except MobilityJournalError:
            raise MobilitySourceError("INVALID_SPEC") from None

    def _fence(
        self, operation: MobilityOperation, lease: DispatchLease, lock
    ) -> tuple[MobilityDispatchContext, MobilityOperation]:
        context = self._context()
        immutable_spec = self._inspect_source(operation.source_artifact_id)
        current = MobilityImmutableFence(
            source_owner_id=context.source_owner_id,
            source_scope_id=context.source_scope_id,
            source_artifact_id=operation.source_artifact_id,
            immutable_spec=immutable_spec,
            destination_scope_id=context.destination_scope_id,
            credential_generation=context.credential_generation,
            destination_binding_hmac=context.destination_binding_hmac,
        )
        checked = self._repository.assert_immutable_fence(
            lease, current, lock=lock, now=self._now()
        )
        return context, checked

    def _prepare_peer(
        self, operation: MobilityOperation, lease: DispatchLease, lock
    ) -> tuple[MobilityDispatchContext, MobilityOperation, DispatchLease]:
        context, operation = self._fence(operation, lease, lock)
        lease = self._repository.renew_dispatch(
            lease,
            lock=lock,
            now=self._now(),
            lease_seconds=context.attempt_lease_seconds,
        )
        return context, operation, lease

    def _peer_call(
        self,
        operation: MobilityOperation,
        lease: DispatchLease,
        lock,
        method,
        *args,
        request_targets: tuple[tuple[str, str], ...],
    ):
        context, operation, lease = self._prepare_peer(operation, lease, lock)
        request_fences = _AttemptRequestFences(
            repository=self._repository,
            lease=lease,
            lock=lock,
            now=self._now,
            lease_seconds=context.attempt_lease_seconds,
            targets=request_targets,
        )
        try:
            with self._peer._request_fence_scope(request_fences):
                try:
                    result = method(*args)
                except BaseException:
                    request_fences.close()
                    raise
                lease = request_fences.finish()
        except (MobilityJournalError, TransferError):
            raise
        except Exception:
            raise TransferError("MOBILITY_UNAVAILABLE") from None
        finally:
            request_fences.close()
        return result, operation, lease

    @staticmethod
    def _same_json(left: object, right: object) -> bool:
        try:
            left_bytes = json.dumps(
                left, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
            right_bytes = json.dumps(
                right, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
            return hmac.compare_digest(left_bytes, right_bytes)
        except (TypeError, ValueError, UnicodeError):
            return False

    def _validate_attestation(
        self, value: object, operation: MobilityOperation
    ) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != self._ATTESTATION_FIELDS:
            raise TransferError("MOBILITY_INTEGRITY")
        supplied_digest = value.get("sha256")
        fields = {name: value[name] for name in self._ATTESTATION_FIELDS - {"sha256"}}
        try:
            canonical = recipient_mobility_attestation(fields)
        except TransferError:
            raise TransferError("MOBILITY_INTEGRITY") from None
        if (
            not isinstance(supplied_digest, str)
            or not hmac.compare_digest(supplied_digest, canonical["sha256"])
            or canonical["authorized_source_owner_id"] != operation.source_owner_id
            or canonical["can_write"] is not True
            or operation.immutable_spec["size_bytes"] > canonical["max_object_bytes"]
        ):
            raise TransferError("MOBILITY_INTEGRITY")
        return canonical

    def _validate_envelope(
        self,
        value: object,
        *,
        operation: MobilityOperation,
        attestation: dict[str, object],
        previous: ReceiptCheckpoint | None,
    ) -> tuple[dict[str, object], ReceiptCheckpoint]:
        spec = dict(operation.immutable_spec)
        if (
            not isinstance(value, dict)
            or set(value) != self._ENVELOPE_FIELDS
            or value.get("protocol_version") != MOBILITY_V1_VERSION
            or value.get("command_id") != operation.remote_command_id
            or not self._same_json(value.get("recipient_attestation"), attestation)
            or not self._same_json(value.get("spec"), spec)
        ):
            raise TransferError("MOBILITY_INTEGRITY")
        receipt = value.get("receipt")
        if not isinstance(receipt, dict):
            raise TransferError("MOBILITY_INTEGRITY")
        state = receipt.get("state")
        expected_fields = self._RECEIPT_FIELDS | (
            {"artifact"} if state == "sealed" else set()
        )
        if set(receipt) != expected_fields or state not in {
            "open",
            "verifying",
            "sealed",
        }:
            raise TransferError("MOBILITY_INTEGRITY")
        try:
            checkpoint = ReceiptCheckpoint(
                transfer_id=receipt.get("transfer_id"),
                artifact_id=(
                    receipt.get("artifact", {}).get("artifact_id")
                    if state == "sealed" and isinstance(receipt.get("artifact"), dict)
                    else None
                ),
                state=state,
                offset=receipt.get("offset"),
                chunk_bytes=receipt.get("chunk_bytes"),
                revision=receipt.get("revision"),
                expires_at=receipt.get("expires_at"),
            )
        except MobilityJournalError:
            raise TransferError("MOBILITY_INTEGRITY") from None
        if (
            checkpoint.offset > spec["size_bytes"]
            or (previous is not None and checkpoint.transfer_id != previous.transfer_id)
            or (previous is not None and checkpoint.offset < previous.offset)
            or (previous is not None and checkpoint.revision < previous.revision)
            or (
                previous is not None
                and previous.state == "verifying"
                and checkpoint.state == "open"
            )
            or (
                checkpoint.state in {"verifying", "sealed"}
                and checkpoint.offset != spec["size_bytes"]
            )
        ):
            raise TransferError("MOBILITY_INTEGRITY")
        if checkpoint.expires_at <= self._now():
            raise TransferError("MOBILITY_RECEIPT_EXPIRED")
        if state == "sealed":
            artifact = receipt.get("artifact")
            if (
                not isinstance(artifact, dict)
                or set(artifact) != {
                    "artifact_id",
                    "sha256",
                    "size_bytes",
                    "media_type",
                }
                or not self._same_json(
                    {
                        name: artifact[name]
                        for name in ("sha256", "size_bytes", "media_type")
                    },
                    spec,
                )
                or checkpoint.offset != spec["size_bytes"]
            ):
                raise TransferError("MOBILITY_INTEGRITY")
        return dict(value), checkpoint

    @staticmethod
    def _validate_range(
        value: object,
        *,
        operation: MobilityOperation,
        offset: int,
        length: int,
    ) -> bytes:
        if not isinstance(value, SourceArtifactRange):
            raise MobilitySourceError("INVALID_SPEC")
        body = value.body
        if (
            value.source_artifact_id != operation.source_artifact_id
            or value.sha256 != operation.immutable_spec["sha256"]
            or value.size_bytes != operation.immutable_spec["size_bytes"]
            or value.offset != offset
            or not isinstance(body, bytes)
            or len(body) != length
            or value.chunk_sha256 != hashlib.sha256(body).hexdigest()
        ):
            raise MobilitySourceError("INVALID_SPEC")
        return body

    @staticmethod
    def _validate_ack(
        value: object, *, checkpoint: ReceiptCheckpoint, body: bytes
    ) -> None:
        digest = hashlib.sha256(body).hexdigest()
        if (
            not isinstance(value, dict)
            or set(value) != ArtifactMobilityDispatchService._ACK_FIELDS
            or value.get("offset") != checkpoint.offset
            or value.get("next_offset") != checkpoint.offset + len(body)
            or value.get("chunk_sha256") != digest
            or type(value.get("revision")) is not int
            or value["revision"] != checkpoint.revision + 1
        ):
            raise TransferError("MOBILITY_INTEGRITY")

    def _transition(
        self,
        lease: DispatchLease,
        lock,
        target: str,
        outcome: str,
        receipt: ReceiptCheckpoint | None,
    ) -> MobilityOperation:
        return self._repository.transition_with_lease(
            lease,
            target,
            lock=lock,
            now=self._now(),
            receipt=receipt,
            outcome_code=outcome,
        )

    def _source_failure(
        self,
        error: MobilitySourceError,
        lease: DispatchLease,
        lock,
        receipt: ReceiptCheckpoint | None,
    ) -> MobilityOperation:
        code = (
            error.args[0]
            if error.args and isinstance(error.args[0], str)
            else "UNAVAILABLE"
        )
        if code == "UNAVAILABLE":
            return self._transition(
                lease, lock, "retryable_blocked", "SOURCE_UNAVAILABLE", receipt
            )
        return self._transition(
            lease, lock, "terminal_blocked", "IMMUTABLE_FENCE", receipt
        )

    def _peer_failure(
        self,
        error: TransferError,
        lease: DispatchLease,
        lock,
        receipt: ReceiptCheckpoint | None,
        *,
        response_may_be_lost: bool,
    ) -> MobilityOperation:
        code = (
            error.args[0]
            if error.args and isinstance(error.args[0], str)
            else "MOBILITY_UNAVAILABLE"
        )
        if code in {"MOBILITY_QUOTA", "MOBILITY_CAPACITY", "MOBILITY_BUSY"}:
            return self._transition(lease, lock, "retryable_blocked", code, receipt)
        if code == "MOBILITY_UNAVAILABLE":
            return self._transition(
                lease,
                lock,
                (
                    "retryable_blocked"
                    if isinstance(error, MobilityPeerAvailabilityError)
                    else "resumable" if response_may_be_lost else "retryable_blocked"
                ),
                code,
                receipt,
            )
        if code == "MOBILITY_RECEIPT_EXPIRED":
            return self._transition(lease, lock, "expired", code, receipt)
        if code in {"MOBILITY_FORBIDDEN", "FORBIDDEN"}:
            outcome = "MOBILITY_FORBIDDEN"
        elif code == "MOBILITY_PROTOCOL":
            outcome = "MOBILITY_PROTOCOL"
        else:
            outcome = "MOBILITY_INTEGRITY"
        return self._transition(lease, lock, "terminal_blocked", outcome, receipt)

    def _attempt(self, original: MobilityOperation) -> MobilityOperation:
        with self._repository.try_acquire_dispatch_lock(original.operation_id) as lock:
            context = self._context()
            lease = self._repository.acquire_dispatch(
                original.operation_id,
                original.source_owner_id,
                lock=lock,
                now=self._now(),
                lease_seconds=context.attempt_lease_seconds,
            )
            receipt = original.receipt
            try:
                if original.receipt_expires_at <= self._now():
                    return self._transition(
                        lease,
                        lock,
                        "expired",
                        "MOBILITY_RECEIPT_EXPIRED",
                        receipt,
                    )
                context, operation = self._fence(original, lease, lock)
                try:
                    capability = self._journal.receipt_capability_for(
                        operation, credential_material=context.credential_material
                    )
                except MobilityJournalError as error:
                    if error.args in {("INTEGRITY",), ("INVALID_CREDENTIAL",)}:
                        return self._transition(
                            lease,
                            lock,
                            "terminal_blocked",
                            "MOBILITY_INTEGRITY",
                            receipt,
                        )
                    raise
                return self._dispatch(operation, lease, lock, capability)
            except MobilitySourceError as error:
                return self._source_failure(error, lease, lock, receipt)
            except TransferError as error:
                return self._peer_failure(
                    error,
                    lease,
                    lock,
                    receipt,
                    response_may_be_lost=False,
                )
            except MobilityJournalError as error:
                if error.args == ("IMMUTABLE_FENCE",):
                    return self._repository.load_operation(
                        original.operation_id, original.source_owner_id
                    )
                raise

    def _dispatch(
        self,
        operation: MobilityOperation,
        lease: DispatchLease,
        lock,
        capability: str,
    ) -> MobilityOperation:
        receipt = operation.receipt
        response_may_be_lost = False
        try:
            raw_attestation, operation, lease = self._peer_call(
                operation,
                lease,
                lock,
                self._peer.recipient_attestation,
                dict(operation.immutable_spec),
                request_targets=(("GET", _ATTESTATION_PATH),),
            )
            attestation = self._validate_attestation(raw_attestation, operation)

            response_may_be_lost = True
            if receipt is None:
                raw_envelope, operation, lease = self._peer_call(
                    operation,
                    lease,
                    lock,
                    self._peer.begin,
                    dict(operation.immutable_spec),
                    operation.remote_command_id,
                    capability,
                    request_targets=(
                        ("GET", _ATTESTATION_PATH),
                        ("POST", "/v1/artifact-transfers"),
                    ),
                )
            else:
                raw_envelope, operation, lease = self._peer_call(
                    operation,
                    lease,
                    lock,
                    self._peer.inspect_receipt,
                    receipt.transfer_id,
                    operation.remote_command_id,
                    dict(operation.immutable_spec),
                    capability,
                    request_targets=(
                        ("GET", _ATTESTATION_PATH),
                        (
                            "POST",
                            "/v1/artifact-transfers/"
                            + receipt.transfer_id
                            + "/mobility-receipt",
                        ),
                    ),
                )
            envelope, receipt = self._validate_envelope(
                raw_envelope,
                operation=operation,
                attestation=attestation,
                previous=receipt,
            )

            while receipt.state == "open":
                if receipt.offset == operation.immutable_spec["size_bytes"]:
                    raw_envelope, operation, lease = self._peer_call(
                        operation,
                        lease,
                        lock,
                        self._peer.seal,
                        envelope,
                        dict(operation.immutable_spec),
                        operation.remote_command_id + ".seal",
                        capability,
                        request_targets=(
                            ("GET", _ATTESTATION_PATH),
                            (
                                "POST",
                                "/v1/artifact-transfers/"
                                + receipt.transfer_id
                                + "/seal",
                            ),
                        ),
                    )
                    envelope, receipt = self._validate_envelope(
                        raw_envelope,
                        operation=operation,
                        attestation=attestation,
                        previous=receipt,
                    )
                    break

                length = min(
                    receipt.chunk_bytes,
                    operation.immutable_spec["size_bytes"] - receipt.offset,
                )
                try:
                    source_range = self._source_reader.read_range(
                        operation.source_artifact_id, receipt.offset, length
                    )
                except MobilitySourceError:
                    raise
                except Exception:
                    raise MobilitySourceError("UNAVAILABLE") from None
                body = self._validate_range(
                    source_range,
                    operation=operation,
                    offset=receipt.offset,
                    length=length,
                )
                acknowledgement, operation, lease = self._peer_call(
                    operation,
                    lease,
                    lock,
                    self._peer.append,
                    envelope,
                    dict(operation.immutable_spec),
                    body,
                    capability,
                    request_targets=(
                        ("GET", _ATTESTATION_PATH),
                        (
                            "PUT",
                            "/v1/artifact-transfers/"
                            + receipt.transfer_id
                            + "/chunks/"
                            + str(receipt.offset),
                        ),
                    ),
                )
                self._validate_ack(acknowledgement, checkpoint=receipt, body=body)
                acknowledged_offset = acknowledgement["next_offset"]
                acknowledged_revision = acknowledgement["revision"]
                raw_envelope, operation, lease = self._peer_call(
                    operation,
                    lease,
                    lock,
                    self._peer.inspect_receipt,
                    receipt.transfer_id,
                    operation.remote_command_id,
                    dict(operation.immutable_spec),
                    capability,
                    request_targets=(
                        ("GET", _ATTESTATION_PATH),
                        (
                            "POST",
                            "/v1/artifact-transfers/"
                            + receipt.transfer_id
                            + "/mobility-receipt",
                        ),
                    ),
                )
                envelope, receipt = self._validate_envelope(
                    raw_envelope,
                    operation=operation,
                    attestation=attestation,
                    previous=receipt,
                )
                if (
                    receipt.offset != acknowledged_offset
                    or receipt.revision != acknowledged_revision
                ):
                    raise TransferError("MOBILITY_INTEGRITY")

            if receipt.state == "verifying":
                return self._transition(lease, lock, "awaiting_seal", "", receipt)
            if receipt.state == "sealed":
                return self._transition(lease, lock, "sealed", "", receipt)
            raise TransferError("MOBILITY_INTEGRITY")
        except MobilitySourceError as error:
            return self._source_failure(error, lease, lock, receipt)
        except TransferError as error:
            return self._peer_failure(
                error,
                lease,
                lock,
                receipt,
                response_may_be_lost=response_may_be_lost,
            )
        except MobilityJournalError as error:
            if error.args == ("IMMUTABLE_FENCE",):
                return self._repository.load_operation(
                    operation.operation_id, operation.source_owner_id
                )
            raise


__all__ = [
    "ArtifactMobilityDispatchService",
    "ArtifactMobilityJournal",
    "ArtifactMobilityJournalRepository",
    "DISPATCH_ELIGIBLE_STATES",
    "DispatchLease",
    "LEASE_TRANSITIONS",
    "MAX_ATTEMPTS",
    "MAX_RECEIPT_TTL_SECONDS",
    "MOBILITY_STATES",
    "MobilityImmutableFence",
    "MobilityDispatchContext",
    "MobilityJournalError",
    "MobilityOperation",
    "MobilityOperationRequest",
    "ReceiptCapabilityProtector",
    "ReceiptCheckpoint",
    "TERMINAL_STATES",
    "derive_remote_command",
]
