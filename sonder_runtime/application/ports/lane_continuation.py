"""Host-only continuation evidence ports; stored identities are not authority."""

from dataclasses import dataclass
import hashlib
import json
from typing import Protocol
from dataclasses import field
import math

MAX_PROJECTION_BYTES = 65536


def _approval_identity(tool, call_digest, surface, expires_at):
    if (
        not isinstance(tool, str)
        or not 1 <= len(tool) <= 128
        or not isinstance(surface, str)
        or not 1 <= len(surface) <= 128
        or not isinstance(call_digest, str)
        or len(call_digest) != 64
        or any(c not in "0123456789abcdef" for c in call_digest)
        or type(expires_at) not in {int, float}
        or not math.isfinite(expires_at)
    ):
        raise ValueError("invalid exact approval identity")


@dataclass(frozen=True)
class PendingApprovalEvidence:
    tool: str
    call_digest: str
    surface: str
    call_id: str
    expires_at: float

    def __post_init__(self):
        _approval_identity(self.tool, self.call_digest, self.surface, self.expires_at)
        if self.call_id != self.call_digest[:16]:
            raise ValueError("pending display call identity mismatch")


class VerificationApprovalPending(Exception):
    def __init__(self, evidence: PendingApprovalEvidence):
        if not isinstance(evidence, PendingApprovalEvidence):
            raise TypeError("typed pending ledger evidence required")
        self.evidence = evidence
        super().__init__("exact verification approval is pending")


@dataclass(frozen=True)
class GrantedApprovalEvidence:
    tool: str
    call_digest: str
    surface: str
    decision_id: str
    approval_nonce: str
    expires_at: float
    source: str

    def __post_init__(self):
        _approval_identity(self.tool, self.call_digest, self.surface, self.expires_at)
        if (
            self.source not in {"approval", "policy"}
            or not isinstance(self.decision_id, str)
            or not 1 <= len(self.decision_id) <= 256
            or not isinstance(self.approval_nonce, str)
            or len(self.approval_nonce) > 256
            or (self.source == "approval") != bool(self.approval_nonce)
        ):
            raise ValueError("invalid approval spend or policy decision evidence")


@dataclass(frozen=True)
class HostContinuationGrant:
    principal_id: str
    host_conversation_id: str
    grant_id: str
    revision: int
    expires_at: float
    workspace_roots: tuple[str, ...]
    allowed_tools: tuple[str, ...]


@dataclass(frozen=True)
class HostAuthorityCeiling:
    principal_id: str
    auth_level: str
    workspace_roots: tuple[str, ...]
    cloud_allowed: bool
    remote_ollama_allowed: bool
    deadline_monotonic: float


@dataclass(frozen=True)
class ContinuationSelection:
    continuation_id: str
    principal_id: str
    issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class PreparedReattachment:
    continuation_id: str
    parent_session_id: str
    parent_grant_revision: int
    principal_id: str
    host_conversation_id: str
    grant_id: str
    grant_revision: int
    epoch: int
    expires_at: float
    workspace_roots: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    command_id: str
    issuer: object = field(repr=False, compare=False)

    def approval_payload(self):
        from dataclasses import asdict

        value = {k: v for k, v in vars(self).items() if k != "issuer"}
        return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True)
class RecoveryItem:
    continuation_id: str
    parent_session_id: str
    epoch: int
    owner_state: str
    expires_at: float
    authority_state: str
    attachment_state: str = ""
    verification_phase: str = ""
    verification_code: str = ""
    pending_identity: "PendingVerificationIdentity | None" = None
    pending_approval: PendingApprovalEvidence | None = None


@dataclass(frozen=True)
class RecoveryPage:
    items: tuple[RecoveryItem, ...]
    next_cursor: int
    has_more: bool


@dataclass(frozen=True)
class PendingVerificationIdentity:
    continuation_id: str
    verification_id: str
    parent_session_id: str
    parent_grant_revision: int
    generation: int
    bundle_digest: str
    command_id: str
    projection_digest: str
    projection_revision: int


@dataclass(frozen=True)
class ProjectionBinding:
    continuation_id: str
    principal_id: str
    run_id: str
    host_conversation_id: str
    parent_session_id: str
    parent_grant_revision: int
    verification_id: str
    bundle_digest: str
    project_roots: tuple[str, ...]
    revision: int

    def __post_init__(self):
        for value in (
            self.continuation_id,
            self.principal_id,
            self.run_id,
            self.host_conversation_id,
            self.parent_session_id,
            self.verification_id,
        ):
            if not isinstance(value, str) or not 1 <= len(value.encode()) <= 256:
                raise ValueError("projection identity is invalid")
        if (
            type(self.parent_grant_revision) is not int
            or self.parent_grant_revision < 1
            or type(self.revision) is not int
            or self.revision < 1
        ):
            raise ValueError("projection revision is invalid")
        if (
            not isinstance(self.bundle_digest, str)
            or len(self.bundle_digest) != 64
            or any(c not in "0123456789abcdef" for c in self.bundle_digest)
        ):
            raise ValueError("projection bundle digest is invalid")
        roots = self.project_roots
        if (
            not isinstance(roots, tuple)
            or not 1 <= len(roots) <= 16
            or any(
                not isinstance(p, str) or not 1 <= len(p.encode()) <= 4096
                for p in roots
            )
            or roots != tuple(sorted(set(roots)))
        ):
            raise ValueError("projection roots must be bounded, unique and ordered")


class HostProjectionCodec(Protocol):
    """Injected trusted host codec, never exposed as an external JSON endpoint.

    encode/binding must validate the host's private issuer and complete schema.
    decode is called only on bounded digest-checked private-store bytes. The
    codec validates original terminal/evidence fields; it never defaults clean.
    """

    def encode(self, projection: object) -> bytes: ...
    def decode(self, payload: bytes) -> object: ...
    def binding(self, projection: object) -> ProjectionBinding: ...


class HostTerminalResultCodec(HostProjectionCodec, Protocol):
    def certificate_digest(self, result: object) -> str: ...


@dataclass(frozen=True)
class TerminalProjectionReceipt:
    receipt_id: str
    projection_digest: str
    original_projection_digest: str
    certificate_digest: str
    revision: int


@dataclass(frozen=True)
class SealedProjection:
    binding: ProjectionBinding
    payload: bytes
    sha256: str

    def validate(self):
        if not isinstance(self.binding, ProjectionBinding):
            raise ValueError("projection binding required")
        if (
            not isinstance(self.payload, bytes)
            or not 1 <= len(self.payload) <= MAX_PROJECTION_BYTES
        ):
            raise ValueError("projection byte bound exceeded")
        if hashlib.sha256(self.payload).hexdigest() != self.sha256:
            raise ValueError("projection digest mismatch")
        value = json.loads(self.payload)
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if not isinstance(value, dict) or canonical != self.payload:
            raise ValueError("projection must be canonical JSON object bytes")


def seal_projection(
    codec: HostProjectionCodec | None, projection: object, expected: ProjectionBinding
) -> SealedProjection:
    if codec is None:
        raise PermissionError("trusted host projection codec is unavailable")
    if codec.binding(projection) != expected:
        raise PermissionError("projection binding mismatch")
    payload = codec.encode(projection)
    if not isinstance(payload, bytes):
        raise ValueError("projection codec must return bytes")
    sealed = SealedProjection(expected, payload, hashlib.sha256(payload).hexdigest())
    sealed.validate()
    restored = codec.decode(payload)
    if codec.binding(restored) != expected or codec.encode(restored) != payload:
        raise ValueError("projection codec roundtrip mismatch")
    return sealed


def open_projection(
    codec: HostProjectionCodec | None,
    sealed: SealedProjection,
    expected: ProjectionBinding,
) -> object:
    if codec is None:
        raise PermissionError("trusted host projection codec is unavailable")
    sealed.validate()
    if sealed.binding != expected:
        raise PermissionError("projection binding mismatch")
    restored = codec.decode(sealed.payload)
    if codec.binding(restored) != expected or codec.encode(restored) != sealed.payload:
        raise ValueError("projection codec roundtrip mismatch")
    return restored
