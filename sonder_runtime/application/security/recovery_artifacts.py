"""Guarded recovery artifacts and audit evidence.

This service provides operational integrity checks around recovery artifacts;
it does not provide an operating-system security boundary.  The configured
owner is an application-level identity, and a same-user process with write
access can replace both an artifact and its metadata.  Callers must preserve
that limitation in any user-facing security claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Mapping


class RecoveryArtifactError(ValueError):
    """Base error for invalid, unauthorized, or unverifiable artifacts."""


class RecoveryArtifactOwnershipError(RecoveryArtifactError):
    """The requested artifact or actor does not match the configured owner."""


class RecoveryArtifactIntegrityError(RecoveryArtifactError):
    """An artifact or its audit chain cannot be verified."""


_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_META_SUFFIX = ".recovery.json"
_AUDIT_NAME = "audit.jsonl"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise RecoveryArtifactError(f"{field} must be non-empty and NUL-free")


@dataclass(frozen=True, slots=True)
class RecoveryArtifact:
    """The verified logical contents of one recovery artifact."""

    artifact_id: str
    owner: str
    kind: str
    payload: Mapping[str, Any]
    digest: str
    previous_audit_digest: str
    audit_digest: str

    def __post_init__(self) -> None:
        if not _ARTIFACT_ID.fullmatch(self.artifact_id):
            raise RecoveryArtifactError("artifact_id is invalid or too long")
        _require_text(self.owner, "owner")
        _require_text(self.kind, "kind")
        if not isinstance(self.payload, Mapping):
            raise RecoveryArtifactError("payload must be an object")
        if not _DIGEST.fullmatch(self.digest) or not _DIGEST.fullmatch(self.audit_digest):
            raise RecoveryArtifactError("artifact digests must be SHA-256 values")
        if self.previous_audit_digest and not _DIGEST.fullmatch(self.previous_audit_digest):
            raise RecoveryArtifactError("previous_audit_digest must be a SHA-256 value")


@dataclass(frozen=True, slots=True)
class RecoveryArtifactInspection:
    """Verification result with an explicit same-user limitation."""

    artifact: RecoveryArtifact
    path: Path
    owner_matches: bool
    digest_matches: bool
    audit_chain_matches: bool
    tamper_evident_only: bool = True

    @property
    def verified(self) -> bool:
        return self.owner_matches and self.digest_matches and self.audit_chain_matches


class RecoveryArtifactService:
    """Write and verify bounded recovery/audit artifacts under an owned root.

    ``owner`` and ``actor`` are application identities, not OS credentials.
    The service refuses path escapes, symlink/reparse components, malformed
    IDs, oversized payloads, and owner mismatches.  It cannot stop a same-user
    process that can write the root from changing all of those files.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        owner: str,
        max_payload_bytes: int = 1_048_576,
        max_audit_entries: int = 4096,
    ) -> None:
        _require_text(owner, "owner")
        if max_payload_bytes < 1 or max_audit_entries < 1:
            raise RecoveryArtifactError("artifact bounds must be positive")
        raw_root = Path(root)
        if not raw_root.is_absolute():
            raise RecoveryArtifactError("artifact root must be absolute")
        raw_root.mkdir(parents=True, exist_ok=True)
        self._reject_link_components(raw_root)
        self.root = raw_root.resolve(strict=True)
        self.owner = owner
        self.max_payload_bytes = max_payload_bytes
        self.max_audit_entries = max_audit_entries
        self._reject_link_components(self.root)

    @staticmethod
    def _reject_link_components(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            try:
                info = current.lstat()
            except OSError as exc:
                raise RecoveryArtifactError("artifact path cannot be inspected") from exc
            if info.st_mode & 0o170000 == 0o120000 or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
                raise RecoveryArtifactError("artifact path contains a symlink or reparse point")

    def _path(self, artifact_id: str) -> Path:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            raise RecoveryArtifactError("artifact_id is invalid or too long")
        path = self.root / f"{artifact_id}.json"
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise RecoveryArtifactError("artifact path escapes owned root") from exc
        self._reject_link_components(self.root)
        self._reject_existing_link(path)
        return path

    @staticmethod
    def _reject_existing_link(path: Path) -> None:
        if not os.path.lexists(str(path)):
            return
        try:
            info = path.lstat()
        except OSError as exc:
            raise RecoveryArtifactError("artifact path cannot be inspected") from exc
        if info.st_mode & 0o170000 == 0o120000 or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
            raise RecoveryArtifactError("artifact path is a symlink or reparse point")

    @property
    def audit_path(self) -> Path:
        path = self.root / _AUDIT_NAME
        self._reject_existing_link(path)
        return path

    def _metadata_path(self, artifact_id: str) -> Path:
        path = self._path(artifact_id).with_suffix(_META_SUFFIX)
        self._reject_existing_link(path)
        return path

    def _read_audit(self) -> list[dict[str, Any]]:
        path = self.audit_path
        if not path.exists():
            return []
        if path.is_symlink():
            raise RecoveryArtifactIntegrityError("audit path is a symlink")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise RecoveryArtifactIntegrityError("audit file cannot be read") from exc
        if len(lines) > self.max_audit_entries:
            raise RecoveryArtifactIntegrityError("audit entry bound exceeded")
        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RecoveryArtifactIntegrityError("audit contains malformed JSON") from exc
            if not isinstance(value, dict):
                raise RecoveryArtifactIntegrityError("audit entries must be objects")
            entries.append(value)
        return entries

    def _write_new(self, path: Path, data: bytes) -> None:
        if len(data) > self.max_payload_bytes:
            raise RecoveryArtifactError("artifact payload exceeds bound")
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError as exc:
            raise RecoveryArtifactError("artifact write failed") from exc
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def write(self, artifact_id: str, *, actor: str, kind: str, payload: Mapping[str, Any]) -> RecoveryArtifact:
        """Create one artifact and append its chained audit record."""
        if actor != self.owner:
            raise RecoveryArtifactOwnershipError("actor does not own recovery artifacts")
        _require_text(kind, "kind")
        if not isinstance(payload, Mapping):
            raise RecoveryArtifactError("payload must be an object")
        body = {"artifact_id": artifact_id, "owner": self.owner, "kind": kind, "payload": dict(payload)}
        body_bytes = _canonical(body)
        if len(body_bytes) > self.max_payload_bytes:
            raise RecoveryArtifactError("artifact payload exceeds bound")
        artifact_path = self._path(artifact_id)
        metadata_path = self._metadata_path(artifact_id)
        if artifact_path.exists() or metadata_path.exists():
            raise RecoveryArtifactError("artifact already exists")
        entries = self._read_audit()
        previous = entries[-1]["audit_digest"] if entries else ""
        if previous and not _DIGEST.fullmatch(previous):
            raise RecoveryArtifactIntegrityError("existing audit chain is malformed")
        digest = sha256(body_bytes).hexdigest()
        audit_material = {"artifact_digest": digest, "artifact_id": artifact_id, "owner": self.owner, "previous": previous}
        audit_digest = _digest(audit_material)
        metadata = {
            **body,
            "digest": digest,
            "previous_audit_digest": previous,
            "audit_digest": audit_digest,
        }
        self._write_new(artifact_path, body_bytes)
        self._write_new(metadata_path, _canonical(metadata))
        entry = {"artifact_id": artifact_id, "owner": self.owner, "artifact_digest": digest, "previous": previous, "audit_digest": audit_digest}
        audit_data = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        existing_audit = self.audit_path.read_bytes() if self.audit_path.exists() else b""
        self._write_new(self.audit_path, existing_audit + audit_data)
        return RecoveryArtifact(artifact_id, self.owner, kind, dict(payload), digest, previous, audit_digest)

    def inspect(self, artifact_id: str, *, actor: str) -> RecoveryArtifactInspection:
        """Verify ownership, payload digest, metadata, and the complete chain."""
        if actor != self.owner:
            raise RecoveryArtifactOwnershipError("actor does not own recovery artifacts")
        artifact_path = self._path(artifact_id)
        metadata_path = self._metadata_path(artifact_id)
        try:
            body = json.loads(artifact_path.read_text(encoding="utf-8"))
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecoveryArtifactIntegrityError("artifact or metadata cannot be read") from exc
        if not isinstance(body, dict) or not isinstance(metadata, dict):
            raise RecoveryArtifactIntegrityError("artifact and metadata must be objects")
        digest = sha256(_canonical(body)).hexdigest()
        digest_matches = digest == metadata.get("digest")
        owner_matches = body.get("owner") == self.owner and metadata.get("owner") == self.owner
        entries = self._read_audit()
        chain_matches = True
        previous = ""
        matching = None
        for entry in entries:
            expected = _digest({"artifact_digest": entry.get("artifact_digest"), "artifact_id": entry.get("artifact_id"), "owner": entry.get("owner"), "previous": entry.get("previous", "")})
            if entry.get("previous", "") != previous or entry.get("audit_digest") != expected:
                chain_matches = False
                break
            previous = entry["audit_digest"]
            if entry.get("artifact_id") == artifact_id:
                matching = entry
        chain_matches = chain_matches and matching is not None and matching.get("artifact_digest") == digest
        artifact = RecoveryArtifact(
            artifact_id, str(metadata.get("owner", "")), str(metadata.get("kind", "")),
            metadata.get("payload", {}), str(metadata.get("digest", "0" * 64)),
            str(metadata.get("previous_audit_digest", "")), str(metadata.get("audit_digest", "0" * 64)),
        )
        return RecoveryArtifactInspection(artifact, artifact_path, owner_matches, digest_matches, chain_matches)

    def verify(self, artifact_id: str, *, actor: str) -> RecoveryArtifact:
        inspection = self.inspect(artifact_id, actor=actor)
        if not inspection.verified:
            raise RecoveryArtifactIntegrityError(
                "artifact verification failed; same-user tampering remains possible"
            )
        return inspection.artifact


__all__ = [
    "RecoveryArtifact",
    "RecoveryArtifactError",
    "RecoveryArtifactInspection",
    "RecoveryArtifactIntegrityError",
    "RecoveryArtifactOwnershipError",
    "RecoveryArtifactService",
]
