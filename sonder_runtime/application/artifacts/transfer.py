"""Scoped immutable byte transfer. No network identity is inferred from a body."""

from sonder_runtime.application.ports.runtime_threads import ThreadPoolExecutor as owned_runtime_pool

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import re
import threading
import time

_VERIFY_SLOTS = threading.BoundedSemaphore(2)
_READ_SLOTS = threading.BoundedSemaphore(8)
MOBILITY_V1_VERSION = "mobility-v1"
_MOBILITY_ATTESTATION_FIELDS = frozenset(
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
    }
)


class TransferError(RuntimeError):
    """Stable public error code; never carries private payload or host paths."""


def check_digest(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise TransferError("INVALID_DIGEST")


def bounded_int(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise TransferError("INVALID_BOUND")


@dataclass(frozen=True)
class TransferGrant:
    principal_id: str
    project_id: str
    node_id: str
    grant_id: str
    revision: int
    expires_at: float
    can_read: bool
    can_write: bool
    max_object_bytes: int
    quota_bytes: int

    def __post_init__(self):
        for value in (self.principal_id, self.project_id, self.node_id, self.grant_id):
            if not isinstance(value, str) or not value or len(value) > 128:
                raise TransferError("INVALID_GRANT")
        bounded_int(self.revision, 1, 2**63 - 1)
        bounded_int(self.max_object_bytes, 0, 64 * 1024**3)
        bounded_int(self.quota_bytes, 1, 128 * 1024**3)
        if type(self.can_read) is not bool or type(self.can_write) is not bool:
            raise TransferError("INVALID_GRANT")
        if not isinstance(self.expires_at, (int, float)) or not math.isfinite(
            self.expires_at
        ):
            raise TransferError("INVALID_GRANT")

    @property
    def scope_id(self):
        import json

        return hashlib.sha256(
            json.dumps(
                [self.principal_id, self.project_id, self.node_id],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class TransferLimits:
    chunk_bytes: int = 1024 * 1024
    max_object_bytes: int = 256 * 1024 * 1024
    total_bytes: int = 2 * 1024**3
    active_per_scope: int = 4
    active_total: int = 8
    ttl_seconds: int = 3600

    def __post_init__(self):
        bounded_int(self.chunk_bytes, 65536, 1024 * 1024)
        bounded_int(self.max_object_bytes, 0, 64 * 1024**3)
        bounded_int(self.total_bytes, 1, 128 * 1024**3)
        bounded_int(self.active_per_scope, 1, 64)
        bounded_int(self.active_total, 1, 256)
        bounded_int(self.ttl_seconds, 1, 86400)
        if self.max_object_bytes > self.chunk_bytes * 65536:
            raise TransferError("INVALID_BOUND")


@dataclass(frozen=True)
class ArtifactRange:
    artifact_id: str
    sha256: str
    size_bytes: int
    offset: int
    body: bytes
    chunk_sha256: str

    @property
    def length(self):
        return len(self.body)


@dataclass(frozen=True)
class MobilityV1Contract:
    """Receiver-issued, request-scoped verifier material for mobility-v1.

    The receipt key is derived from the current receiver bearer and an incoming
    256-bit capability. It is never serialized, returned, or included in repr.
    """

    _attestation_json: str = field(repr=False)
    _receipt_key: bytes = field(repr=False, compare=False)
    _digest: str = field(repr=False)

    @classmethod
    def issue(cls, fields, receipt_key):
        encoded = _canonical_mobility_attestation(fields)
        if not isinstance(receipt_key, bytes) or len(receipt_key) != 32:
            raise TransferError("INVALID_MOBILITY_CONTRACT")
        return cls(encoded, receipt_key, hashlib.sha256(encoded.encode("ascii")).hexdigest())

    def recipient_attestation(self):
        return {**json.loads(self._attestation_json), "sha256": self._digest}

    def matches_grant(self, grant):
        fields = json.loads(self._attestation_json)
        return (
            fields["principal_id"] == grant.principal_id
            and fields["project_id"] == grant.project_id
            and fields["authorized_source_owner_id"] == grant.node_id
            and fields["grant_id"] == grant.grant_id
            and fields["grant_revision"] == grant.revision
            and fields["can_write"] is grant.can_write
            and fields["max_object_bytes"] == grant.max_object_bytes
        )

    def verifier_for(self, scope_id, transfer_id, command_id):
        if not isinstance(scope_id, str) or not re.fullmatch("[0-9a-f]{64}", scope_id):
            raise TransferError("FORBIDDEN")
        if not isinstance(transfer_id, str) or not re.fullmatch("[0-9a-f]{32}", transfer_id):
            raise TransferError("FORBIDDEN")
        if not isinstance(command_id, str) or not re.fullmatch("[A-Za-z0-9_.:-]{1,128}", command_id):
            raise TransferError("FORBIDDEN")
        material = json.dumps(
            [MOBILITY_V1_VERSION, self._digest, scope_id, transfer_id, command_id],
            separators=(",", ":"),
        ).encode("ascii")
        return hmac.new(self._receipt_key, material, hashlib.sha256).hexdigest()


def mobility_envelope(record, contract):
    if not isinstance(record, dict) or set(record) != {"command_id", "spec", "receipt"}:
        raise TransferError("INVALID_MOBILITY_CONTRACT")
    return {
        "protocol_version": MOBILITY_V1_VERSION,
        "recipient_attestation": contract.recipient_attestation(),
        "command_id": record["command_id"],
        "spec": record["spec"],
        "receipt": record["receipt"],
    }


def recipient_mobility_attestation(fields):
    encoded = _canonical_mobility_attestation(fields)
    return {
        **json.loads(encoded),
        "sha256": hashlib.sha256(encoded.encode("ascii")).hexdigest(),
    }


def _canonical_mobility_attestation(fields):
    if not isinstance(fields, dict) or set(fields) != _MOBILITY_ATTESTATION_FIELDS:
        raise TransferError("INVALID_MOBILITY_CONTRACT")
    if fields.get("protocol_version") != MOBILITY_V1_VERSION:
        raise TransferError("INVALID_MOBILITY_CONTRACT")
    for name in (
        "receiver_identity_id",
        "principal_id",
        "project_id",
        "authorized_source_owner_id",
        "grant_id",
    ):
        value = fields.get(name)
        if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(
            ord(char) < 33 or ord(char) > 126 for char in value
        ):
            raise TransferError("INVALID_MOBILITY_CONTRACT")
    bounded_int(fields.get("grant_revision"), 1, 2**63 - 1)
    bounded_int(fields.get("max_object_bytes"), 0, 64 * 1024**3)
    if type(fields.get("can_write")) is not bool:
        raise TransferError("INVALID_MOBILITY_CONTRACT")
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class ArtifactTransferService:
    def __init__(self, store, *, authorizer=None, limits=TransferLimits()):
        self.store, self.authorizer, self.limits = store, authorizer, limits
        self._workers = owned_runtime_pool(
            max_workers=1, thread_name_prefix="artifact-verify"
        )
        self._slots = _VERIFY_SLOTS

    def _grant(self, context, action):
        if self.authorizer is None:
            raise TransferError("UNAVAILABLE")
        if context.expired or context.cancellation.cancelled:
            raise TransferError("FORBIDDEN")
        try:
            grant = self.authorizer(context, action)
        except PermissionError:
            raise TransferError("FORBIDDEN") from None
        except Exception:
            raise TransferError("UNAVAILABLE") from None
        if (
            not isinstance(grant, TransferGrant)
            or grant.principal_id != context.principal_id
            or grant.expires_at <= time.time()
            or not (grant.can_read if action == "read" else grant.can_write)
        ):
            raise TransferError("FORBIDDEN")
        return grant

    def _validate_spec(self, spec, grant):
        if not isinstance(spec, dict) or set(spec) != {
            "sha256",
            "size_bytes",
            "media_type",
        }:
            raise TransferError("INVALID_SPEC")
        check_digest(spec["sha256"])
        bounded_int(
            spec["size_bytes"],
            0,
            min(grant.max_object_bytes, self.limits.max_object_bytes),
        )
        media = spec["media_type"]
        if (
            not isinstance(media, str)
            or not 1 <= len(media) <= 128
            or any(ord(c) < 32 for c in media)
        ):
            raise TransferError("INVALID_SPEC")

    def begin_upload(self, spec, command_id, context):
        grant = self._grant(context, "write")
        self._validate_spec(spec, grant)
        return self.store.begin(spec, command_id, grant, self.limits)

    def begin_mobility_upload(self, spec, command_id, contract, context):
        grant = self._grant(context, "write")
        self._validate_spec(spec, grant)
        if not isinstance(contract, MobilityV1Contract) or not contract.matches_grant(grant):
            raise TransferError("FORBIDDEN")
        record = self.store.begin_mobility(
            spec, command_id, grant, self.limits,
            lambda scope_id, transfer_id, durable_command: contract.verifier_for(
                scope_id, transfer_id, durable_command
            ),
        )
        return mobility_envelope(record, contract)

    def inspect_mobility_upload(self, transfer_id, command_id, contract, context):
        grant = self._grant(context, "write")
        if not isinstance(contract, MobilityV1Contract) or not contract.matches_grant(grant):
            raise TransferError("FORBIDDEN")
        record = self.store.inspect_mobility(
            transfer_id,
            command_id,
            grant,
            lambda scope_id, durable_transfer_id, supplied_command: contract.verifier_for(
                scope_id, durable_transfer_id, supplied_command
            ),
        )
        return mobility_envelope(record, contract)

    def inspect_upload(self, transfer_id, context):
        return self.store.inspect(transfer_id, self._grant(context, "read"))

    def append_chunk(self, transfer_id, offset, chunk_sha256, body, context):
        grant = self._grant(context, "write")
        bounded_int(offset, 0, self.limits.max_object_bytes)
        check_digest(chunk_sha256)
        if not isinstance(body, bytes) or not 1 <= len(body) <= self.limits.chunk_bytes:
            raise TransferError("INVALID_BOUND")
        if hashlib.sha256(body).hexdigest() != chunk_sha256:
            raise TransferError("CHUNK_DIGEST_MISMATCH")
        return self.store.append(transfer_id, offset, chunk_sha256, body, grant)

    def append_mobility_chunk(
        self, transfer_id, offset, chunk_sha256, body, contract, context
    ):
        grant = self._grant(context, "write")
        if not isinstance(contract, MobilityV1Contract) or not contract.matches_grant(grant):
            raise TransferError("FORBIDDEN")
        bounded_int(offset, 0, self.limits.max_object_bytes)
        check_digest(chunk_sha256)
        if not isinstance(body, bytes) or not 1 <= len(body) <= self.limits.chunk_bytes:
            raise TransferError("INVALID_BOUND")
        if hashlib.sha256(body).hexdigest() != chunk_sha256:
            raise TransferError("CHUNK_DIGEST_MISMATCH")
        return self.store.append(
            transfer_id,
            offset,
            chunk_sha256,
            body,
            grant,
            mobility=True,
            verifier_for=lambda scope_id, durable_transfer_id, durable_command: contract.verifier_for(
                scope_id, durable_transfer_id, durable_command
            ),
        )

    def _seal_upload(
        self, transfer_id, command_id, context, grant, *, mobility=False, verifier_for=None
    ):
        # Durable read before worker admission, including idempotent sealed replay.
        if mobility:
            receipt = self.store.mobility_record(transfer_id, grant, verifier_for)["receipt"]
        else:
            receipt = self.store.inspect(transfer_id, grant)
        if receipt["state"] == "sealed":
            return self.store.admit_seal(
                transfer_id,
                command_id,
                grant,
                mobility=mobility,
                verifier_for=verifier_for,
            )
        if not self._slots.acquire(blocking=False):
            raise TransferError("BUSY")
        try:
            receipt = self.store.admit_seal(
                transfer_id,
                command_id,
                grant,
                mobility=mobility,
                verifier_for=verifier_for,
            )

            def verify():
                try:
                    while True:
                        self._grant(context, "write")
                        try:
                            self.store.seal(
                                transfer_id,
                                grant,
                                lambda: self._grant(context, "write"),
                                mobility=mobility,
                            )
                            break
                        except TransferError as error:
                            if str(error) != "BUSY":
                                raise
                            time.sleep(0.05)
                except Exception:
                    # Status remains resumable; exceptions never enter public metadata.
                    pass
                finally:
                    self._slots.release()

            self._workers.submit(verify)
            return receipt
        except BaseException:
            self._slots.release()
            raise

    def seal_upload(self, transfer_id, command_id, context):
        grant = self._grant(context, "write")
        return self._seal_upload(transfer_id, command_id, context, grant)

    def seal_mobility_upload(self, transfer_id, command_id, contract, context):
        grant = self._grant(context, "write")
        if not isinstance(contract, MobilityV1Contract) or not contract.matches_grant(grant):
            raise TransferError("FORBIDDEN")
        verifier_for = lambda scope_id, durable_transfer_id, durable_command: contract.verifier_for(
            scope_id, durable_transfer_id, durable_command
        )
        self._seal_upload(
            transfer_id,
            command_id,
            context,
            grant,
            mobility=True,
            verifier_for=verifier_for,
        )
        return mobility_envelope(
            self.store.mobility_record(transfer_id, grant, verifier_for), contract
        )

    def abort_upload(self, transfer_id, command_id, context):
        return self.store.abort(transfer_id, command_id, self._grant(context, "write"))

    def abort_mobility_upload(self, transfer_id, command_id, contract, context):
        grant = self._grant(context, "write")
        if not isinstance(contract, MobilityV1Contract) or not contract.matches_grant(grant):
            raise TransferError("FORBIDDEN")
        verifier_for = lambda scope_id, durable_transfer_id, durable_command: contract.verifier_for(
            scope_id, durable_transfer_id, durable_command
        )
        self.store.abort(
            transfer_id,
            command_id,
            grant,
            mobility=True,
            verifier_for=verifier_for,
        )
        return mobility_envelope(
            self.store.mobility_record(transfer_id, grant, verifier_for), contract
        )

    def inspect_artifact(self, artifact_id, context):
        return self.store.artifact(artifact_id, self._grant(context, "read"))

    def read_range(self, artifact_id, offset, length, context):
        grant = self._grant(context, "read")
        bounded_int(offset, 0, self.limits.max_object_bytes)
        bounded_int(length, 1, self.limits.chunk_bytes)
        if not _READ_SLOTS.acquire(blocking=False):
            raise TransferError("BUSY")
        try:
            return self.store.read_range(artifact_id, offset, length, grant)
        finally:
            _READ_SLOTS.release()

    def close(self):
        self._workers.shutdown(wait=True)
