"""Scoped immutable byte transfer. No network identity is inferred from a body."""

from sonder_runtime.application.ports.runtime_threads import ThreadPoolExecutor as owned_runtime_pool

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import math
import re
import threading
import time

_VERIFY_SLOTS = threading.BoundedSemaphore(2)
_READ_SLOTS = threading.BoundedSemaphore(8)


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

    def begin_upload(self, spec, command_id, context):
        grant = self._grant(context, "write")
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
        return self.store.begin(spec, command_id, grant, self.limits)

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

    def seal_upload(self, transfer_id, command_id, context):
        grant = self._grant(context, "write")
        # Durable read before worker admission, including idempotent sealed replay.
        receipt = self.store.inspect(transfer_id, grant)
        if receipt["state"] == "sealed":
            return self.store.admit_seal(transfer_id, command_id, grant)
        if not self._slots.acquire(blocking=False):
            raise TransferError("BUSY")
        try:
            receipt = self.store.admit_seal(transfer_id, command_id, grant)

            def verify():
                try:
                    while True:
                        self._grant(context, "write")
                        try:
                            self.store.seal(
                                transfer_id,
                                grant,
                                lambda: self._grant(context, "write"),
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

    def abort_upload(self, transfer_id, command_id, context):
        return self.store.abort(transfer_id, command_id, self._grant(context, "write"))

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
