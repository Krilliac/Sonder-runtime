"""Bounded, platform-neutral update state and TUF-like metadata contracts.

This module is intentionally an application boundary.  It coordinates
immutable metadata and injected download/staging/health/activation ports; it
does not open a network connection, execute a helper, or mutate a release
pointer itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
from typing import Callable, Protocol

from .release_evidence import (
    ActivationRequest,
    AtomicReleaseActivator,
    ReleaseEvidencePackage,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _hash(value: str, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value.lower()):
        raise ValueError(f"{field} must be a SHA-256 digest")


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")


class MetadataChainError(ValueError):
    """A bounded metadata chain is malformed, stale, or not trusted."""


@dataclass(frozen=True, slots=True)
class TufLikeMetadata:
    """The small immutable subset of TUF metadata needed by update clients."""

    role: str
    version: int
    expires_at: str
    payload_digest: str
    signer: str
    signature: str
    previous_digest: str = ""
    target_digests: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.role not in {"root", "timestamp", "snapshot", "targets"}:
            raise ValueError("unsupported TUF metadata role")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("metadata version must be a positive integer")
        _text(self.expires_at, "metadata expiry")
        _hash(self.payload_digest, "metadata payload_digest")
        _text(self.signer, "metadata signer")
        _text(self.signature, "metadata signature")
        if self.previous_digest:
            _hash(self.previous_digest, "metadata previous_digest")
        names = [name for name, _ in self.target_digests]
        if len(names) != len(set(names)) or any(not name for name in names):
            raise ValueError("metadata target names must be unique and non-empty")
        for name, digest in self.target_digests:
            _hash(digest, f"target {name}")

    def signing_bytes(self) -> bytes:
        return _canonical({
            "role": self.role,
            "version": self.version,
            "expires_at": self.expires_at,
            "payload_digest": self.payload_digest,
            "previous_digest": self.previous_digest,
            "target_digests": self.target_digests,
            "signer": self.signer,
        })

    @property
    def digest(self) -> str:
        return sha256(self.signing_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class TufLikeMetadataChain:
    """An ordered, bounded root→timestamp→snapshot→targets chain."""

    entries: tuple[TufLikeMetadata, ...]
    max_entries: int = 4

    def __post_init__(self) -> None:
        if type(self.max_entries) is not int or self.max_entries < 4:
            raise ValueError("metadata max_entries must be at least four")
        if not self.entries or len(self.entries) > self.max_entries:
            raise ValueError("metadata chain is empty or exceeds its bound")
        roles = tuple(item.role for item in self.entries)
        required = ("root", "timestamp", "snapshot", "targets")
        if roles[:4] != required:
            raise ValueError("metadata chain must begin root/timestamp/snapshot/targets")
        if len(set(roles)) != len(roles):
            raise ValueError("metadata roles may occur only once")
        for prior, current in zip(self.entries, self.entries[1:]):
            if current.previous_digest != prior.digest:
                raise ValueError("metadata chain link does not match prior digest")
            if current.version < prior.version:
                raise ValueError("metadata versions must not move backwards")

    @property
    def digest(self) -> str:
        return _digest(tuple(item.digest for item in self.entries))

    def verify(
        self,
        verifier: Callable[[bytes, str, str], bool],
        *,
        now: datetime | None = None,
        max_targets: int = 1024,
    ) -> None:
        if type(max_targets) is not int or max_targets < 1:
            raise MetadataChainError("max_targets must be positive")
        current_time = now or datetime.now(timezone.utc)
        for entry in self.entries:
            try:
                expiry = datetime.fromisoformat(entry.expires_at.replace("Z", "+00:00"))
            except ValueError:
                raise MetadataChainError("metadata expiry is not an ISO-8601 timestamp") from None
            if expiry.tzinfo is None or expiry <= current_time:
                raise MetadataChainError(f"{entry.role} metadata is expired")
            if not verifier(entry.signing_bytes(), entry.signature, entry.signer):
                raise MetadataChainError(f"{entry.role} metadata signature rejected")
        targets = self.entries[-1].target_digests
        if len(targets) > max_targets:
            raise MetadataChainError("metadata target count exceeds bound")


class UpdatePhase(Enum):
    AVAILABLE = "available"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    VERIFIED = "verified"
    STAGED = "staged"
    HEALTH_CHECKED = "health_checked"
    ACTIVATING = "activating"
    ACTIVATED = "activated"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UpdateTarget:
    update_id: str
    release_id: str
    version: str
    artifact_digest: str
    evidence: ReleaseEvidencePackage
    metadata: TufLikeMetadataChain

    def __post_init__(self) -> None:
        _text(self.update_id, "update_id")
        _text(self.release_id, "release_id")
        _text(self.version, "version")
        _hash(self.artifact_digest, "artifact_digest")
        if self.artifact_digest not in dict(self.evidence.manifest.artifact_hashes).values():
            raise ValueError("artifact digest is not present in signed release manifest")


@dataclass(frozen=True, slots=True)
class UpdateSnapshot:
    target: UpdateTarget
    phase: UpdatePhase
    revision: int
    artifact_digest: str = ""
    staged_ref: str = ""
    health_ok: bool = False
    history: tuple[UpdatePhase, ...] = ()


class UpdatePort(Protocol):
    def download(self, target: UpdateTarget) -> bytes: ...
    def stage(self, target: UpdateTarget, artifact: bytes) -> str: ...
    def health_check(self, target: UpdateTarget, staged_ref: str) -> bool: ...


class BoundedUpdateState:
    """Lifecycle coordinator with bounded, append-only in-memory history."""

    def __init__(self, target: UpdateTarget, *, max_history: int = 32) -> None:
        if type(max_history) is not int or max_history < 1:
            raise ValueError("max_history must be positive")
        self._max_history = max_history
        self._snapshot = UpdateSnapshot(target, UpdatePhase.AVAILABLE, 0)

    @property
    def snapshot(self) -> UpdateSnapshot:
        return self._snapshot

    def _move(self, phase: UpdatePhase, **changes: object) -> UpdateSnapshot:
        history = self._snapshot.history + (self._snapshot.phase,)
        if len(history) > self._max_history:
            history = history[-self._max_history:]
        self._snapshot = UpdateSnapshot(
            self._snapshot.target, phase, self._snapshot.revision + 1,
            changes.get("artifact_digest", self._snapshot.artifact_digest),
            changes.get("staged_ref", self._snapshot.staged_ref),
            changes.get("health_ok", self._snapshot.health_ok), history,
        )
        return self._snapshot

    def download(self, port: UpdatePort) -> UpdateSnapshot:
        if self.snapshot.phase is not UpdatePhase.AVAILABLE:
            raise ValueError("download requires available update")
        self._move(UpdatePhase.DOWNLOADING)
        artifact = port.download(self.snapshot.target)
        if not isinstance(artifact, bytes):
            self._move(UpdatePhase.FAILED)
            raise ValueError("download port must return bytes")
        digest = sha256(artifact).hexdigest()
        if digest != self.snapshot.target.artifact_digest:
            self._move(UpdatePhase.FAILED, artifact_digest=digest)
            raise ValueError("downloaded artifact digest mismatch")
        return self._move(UpdatePhase.DOWNLOADED, artifact_digest=digest)

    def verify(self, verifier: Callable[[bytes, str, str], bool], *, now: datetime | None = None) -> UpdateSnapshot:
        if self.snapshot.phase is not UpdatePhase.DOWNLOADED:
            raise ValueError("verification requires downloaded update")
        self.snapshot.target.metadata.verify(verifier, now=now)
        self.snapshot.target.evidence.verify(verifier)
        return self._move(UpdatePhase.VERIFIED)

    def stage(self, port: UpdatePort, artifact: bytes) -> UpdateSnapshot:
        if self.snapshot.phase is not UpdatePhase.VERIFIED:
            raise ValueError("staging requires verified update")
        if not isinstance(artifact, bytes) or sha256(artifact).hexdigest() != self.snapshot.target.artifact_digest:
            self._move(UpdatePhase.FAILED)
            raise ValueError("staged artifact digest mismatch")
        staged_ref = port.stage(self.snapshot.target, artifact)
        if not isinstance(staged_ref, str) or not staged_ref:
            raise ValueError("stage port must return a bounded reference")
        return self._move(UpdatePhase.STAGED, staged_ref=staged_ref)

    def health_gate(self, port: UpdatePort) -> UpdateSnapshot:
        if self.snapshot.phase is not UpdatePhase.STAGED:
            raise ValueError("health gate requires staged update")
        ok = port.health_check(self.snapshot.target, self.snapshot.staged_ref)
        if not ok:
            self._move(UpdatePhase.FAILED, health_ok=False)
            raise RuntimeError("update health gate failed")
        return self._move(UpdatePhase.HEALTH_CHECKED, health_ok=True)

    def activate(self, activator: AtomicReleaseActivator, request: ActivationRequest) -> UpdateSnapshot:
        if self.snapshot.phase is not UpdatePhase.HEALTH_CHECKED or not self.snapshot.health_ok:
            raise ValueError("activation requires a successful health gate")
        self._move(UpdatePhase.ACTIVATING)
        try:
            activator.activate(request)
        except Exception:
            self._move(UpdatePhase.FAILED)
            raise
        return self._move(UpdatePhase.ACTIVATED)

    def rollback(self, rollback: Callable[[UpdateTarget], None]) -> UpdateSnapshot:
        if self.snapshot.phase is not UpdatePhase.ACTIVATED:
            raise ValueError("rollback requires an activated update")
        self._move(UpdatePhase.ROLLING_BACK)
        try:
            rollback(self.snapshot.target)
        except Exception:
            self._move(UpdatePhase.FAILED)
            raise
        return self._move(UpdatePhase.ROLLED_BACK)


__all__ = [
    "BoundedUpdateState", "MetadataChainError", "TufLikeMetadata",
    "TufLikeMetadataChain", "UpdatePhase", "UpdatePort", "UpdateSnapshot",
    "UpdateTarget",
]
