"""Cooperative trusted-in-process ports for a private sealed artifact source.

These protocols describe host-owned internal APIs; they are not Python
object-capability security boundaries. Runtime composition must inject a
publisher or reader only into approved local workload code. No model, MCP,
HTTP, CLI, REPL, or public-plugin path receives either port. Any untrusted
extension or model code must be process-isolated before a source authority is
injected, because code already able to reflect over this interpreter is outside
this boundary.

The binding still verifies its issued context, source scope, role, and trusted
publisher provenance on every operation. Those checks prevent accidental
cross-role wiring and forged ordinary call inputs; they do not sandbox code
that already controls the same interpreter.
"""

from typing import Protocol

from ..artifacts.mobility import (
    DispatchLease,
    MobilityImmutableFence,
    MobilityOperation,
    ReceiptCapabilityProtector,
    ReceiptCheckpoint,
    _ArtifactMobilityPeerRequestFences,
)
from ..artifacts.mobility_source import SourceArtifactRange


class ArtifactMobilityPublisher(Protocol):
    """Trusted host-injected local source publisher."""

    def publish_sealed(
        self, stream, immutable_spec: dict, trusted_provenance: object
    ) -> dict: ...


class ArtifactMobilityReader(Protocol):
    """Trusted host-injected local source reader for bounded dispatch."""

    def inspect_sealed(self, source_artifact_id: str) -> dict: ...

    def read_range(
        self, source_artifact_id: str, offset: int, length: int
    ) -> SourceArtifactRange: ...


class ArtifactMobilityPeer(Protocol):
    """Fixed configured destination transport, separate from source authority.

    This is the application-internal transport contract for the durable
    operator-invoked service.  It carries only an already sealed immutable
    spec, receiver receipt envelope, and opaque receipt capability.  It never
    accepts a destination URL, source path, source scope, or source port.
    """

    def _request_fence_scope(
        self, request_fences: _ArtifactMobilityPeerRequestFences
    ): ...

    def recipient_attestation(self, spec: dict) -> dict: ...

    def begin(
        self,
        spec: dict,
        command_id: str,
        receipt_capability: str,
    ) -> dict: ...

    def inspect_receipt(
        self,
        transfer_id: str,
        command_id: str,
        spec: dict,
        receipt_capability: str,
    ) -> dict: ...

    def append(
        self,
        envelope: dict,
        immutable_spec: dict,
        body: bytes,
        receipt_capability: str,
    ) -> dict: ...

    def seal(
        self,
        envelope: dict,
        immutable_spec: dict,
        seal_command_id: str,
        receipt_capability: str,
    ) -> dict: ...


class ArtifactMobilityDispatchLock(Protocol):
    """A held private local OS fence for one bounded attempt.

    This is an internal application port.  It carries no destination, source,
    credential, path, peer, or scheduling authority.  Dispatch code must
    retain it from lease acquisition through every peer call and transition.
    """

    @property
    def held(self) -> bool: ...

    def close(self) -> None: ...


class ArtifactMobilityJournalStore(ReceiptCapabilityProtector, Protocol):
    """Private durable intent/lease port; it never invokes a peer itself."""

    def create_operation(self, operation: MobilityOperation) -> MobilityOperation: ...

    def load_operation(
        self, operation_id: str, source_owner_id: str
    ) -> MobilityOperation: ...

    def load_operation_for_fencing(self, operation_id: str) -> MobilityOperation: ...

    def try_acquire_dispatch_lock(
        self, operation_id: str
    ) -> ArtifactMobilityDispatchLock: ...

    def acquire_dispatch(
        self,
        operation_id: str,
        source_owner_id: str,
        *,
        lock: ArtifactMobilityDispatchLock | None = None,
        now: float,
        lease_seconds: int,
    ) -> DispatchLease: ...

    def renew_dispatch(
        self,
        lease: DispatchLease,
        *,
        lock: ArtifactMobilityDispatchLock | None = None,
        now: float,
        lease_seconds: int,
    ) -> DispatchLease: ...

    def assert_current_lease(
        self,
        lease: DispatchLease,
        *,
        lock: ArtifactMobilityDispatchLock | None = None,
        now: float,
    ) -> MobilityOperation: ...

    def assert_immutable_fence(
        self,
        lease: DispatchLease,
        current: MobilityImmutableFence,
        *,
        lock: ArtifactMobilityDispatchLock | None = None,
        now: float,
    ) -> MobilityOperation: ...

    def transition_with_lease(
        self,
        lease: DispatchLease,
        target_state: str,
        *,
        lock: ArtifactMobilityDispatchLock | None = None,
        now: float,
        receipt: ReceiptCheckpoint | None = None,
        outcome_code: str = "",
    ) -> MobilityOperation: ...

    def recover_expired_leases(self, *, now: float) -> tuple[str, ...]: ...
