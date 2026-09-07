"""Local-only durable intent primitives for one fixed mobility-v1 copy.

This module has deliberately no transport, source-reader, filesystem, route,
or scheduler dependency.  It gives a later explicit application service a
small, typed journal contract: make immutable intent first, then acquire a
real local dispatch lock and a fenced lease before it can contact a peer.

The journal's records are private implementation data.  ``public_status`` is
the only projection intended for a CLI, REPL, log, or status surface.  It
omits destination transport material, credential generation, source scope,
lease token, receipt capability, and receiver receipt details.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
import secrets
import time
from types import MappingProxyType
from typing import Mapping, Protocol

from .transfer import MOBILITY_V1_VERSION

_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[!-~]{1,128}")
_COMMAND = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_PROTECTED_CAPABILITY = re.compile(r"v1\.[0-9a-f]{24}\.[0-9a-f]{96}")
_CAPABILITY = re.compile(r"[0-9a-f]{64}")

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
        "MOBILITY_UNAVAILABLE",
        "MOBILITY_FORBIDDEN",
        "MOBILITY_INTEGRITY",
        "MOBILITY_PROTOCOL",
        "MOBILITY_RECEIPT_EXPIRED",
    }
)


class MobilityJournalError(RuntimeError):
    """A stable local journal code with no paths, credentials, or peer text."""


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


__all__ = [
    "ArtifactMobilityJournal",
    "ArtifactMobilityJournalRepository",
    "DISPATCH_ELIGIBLE_STATES",
    "DispatchLease",
    "LEASE_TRANSITIONS",
    "MAX_ATTEMPTS",
    "MAX_RECEIPT_TTL_SECONDS",
    "MOBILITY_STATES",
    "MobilityImmutableFence",
    "MobilityJournalError",
    "MobilityOperation",
    "MobilityOperationRequest",
    "ReceiptCapabilityProtector",
    "ReceiptCheckpoint",
    "TERMINAL_STATES",
    "derive_remote_command",
]
