"""Signed update activation, health gates, and rollback (WP9)."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Callable


@dataclass(frozen=True)
class SignedManifest:
    version: str
    artifact_digest: str
    signer: str
    signature: str

    def signing_bytes(self) -> bytes:
        return f"{self.version}\n{self.artifact_digest}\n{self.signer}".encode()


@dataclass(frozen=True)
class ActivationRecord:
    manifest: SignedManifest
    artifact_digest: str


class UpdateActivation:
    def __init__(self, verifier: Callable[[bytes, str, str], bool], health_check: Callable[[SignedManifest], bool]) -> None:
        self._verifier = verifier
        self._health_check = health_check
        self._current: ActivationRecord | None = None
        self._history: list[ActivationRecord] = []

    @property
    def current(self) -> ActivationRecord | None:
        return self._current

    @property
    def history(self) -> tuple[ActivationRecord, ...]:
        return tuple(self._history)

    def activate(self, manifest: SignedManifest, artifact: bytes) -> ActivationRecord:
        digest = hashlib.sha256(artifact).hexdigest()
        if digest != manifest.artifact_digest:
            raise ValueError("artifact digest does not match manifest")
        if not self._verifier(manifest.signing_bytes(), manifest.signature, manifest.signer):
            raise ValueError("manifest signature rejected")
        candidate = ActivationRecord(manifest, digest)
        if not self._health_check(manifest):
            raise RuntimeError("update health gate failed")
        if self._current is not None:
            self._history.append(self._current)
        self._current = candidate
        return candidate

    def rollback(self) -> ActivationRecord:
        if not self._history:
            raise RuntimeError("no prior activation to roll back")
        prior = self._history.pop()
        self._current = prior
        return prior
