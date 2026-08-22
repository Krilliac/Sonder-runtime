"""Crash-safe migration and schema-epoch adoption contracts.

This module is deliberately application-owned and storage-neutral.  The
SQLite bridge and backup adapters can use it to make the migration sequence
observable without importing one another:

    verify backup -> adopt/migrate -> prove restore independently -> decide
    whether temporary bridge code may be retired.

The helpers never delete files or migration code.  They produce immutable
proofs and explicit decisions for a composition root or release process to
consume.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol, Sequence


SUPPORTED_SCHEMA_EPOCH = 2


class MigrationSafetyError(RuntimeError):
    """Base error for an unsafe migration transition."""


class BackupVerificationError(MigrationSafetyError):
    """The pre-migration backup could not be independently verified."""


class RestoreVerificationError(MigrationSafetyError):
    """A restore proof could not be established."""


class FutureSchemaError(MigrationSafetyError):
    """State belongs to a schema epoch newer than this runtime supports."""


class BackupVerifier(Protocol):
    """Minimal adapter port for an independently verified backup."""

    def verify(self, backup_path: str | Path) -> Sequence[str]: ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_digests(values: Mapping[str, str]) -> dict[str, str]:
    result = {}
    for name, digest in values.items():
        if not isinstance(name, str) or not name.strip():
            raise MigrationSafetyError("digest path must be a non-empty string")
        if not isinstance(digest, str) or len(digest) != 64:
            raise MigrationSafetyError(f"invalid SHA-256 digest for {name!r}")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise MigrationSafetyError(f"invalid SHA-256 digest for {name!r}") from exc
        result[name] = digest.lower()
    return result


@dataclass(frozen=True)
class BackupProof:
    """Evidence that all named source files were covered by a verified backup."""

    backup_path: str
    verified_at: str
    source_digests: Mapping[str, str]
    backup_manifest_digest: str | None = None

    def __post_init__(self) -> None:
        path = Path(self.backup_path)
        if not path.is_dir():
            raise MigrationSafetyError("backup proof must reference a directory")
        object.__setattr__(self, "source_digests", dict(_normalise_digests(self.source_digests)))

    @property
    def complete(self) -> bool:
        return bool(self.source_digests)


@dataclass(frozen=True)
class RestoreProof:
    """Evidence that restored files independently match the backup proof."""

    backup_path: str
    restore_path: str
    verified_at: str
    restored_digests: Mapping[str, str]

    def __post_init__(self) -> None:
        if not Path(self.restore_path).is_dir():
            raise MigrationSafetyError("restore proof must reference a directory")
        object.__setattr__(self, "restored_digests", dict(_normalise_digests(self.restored_digests)))


@dataclass(frozen=True)
class EpochAdoption:
    """Immutable record of a successful epoch transition."""

    source_epoch: int | None
    target_epoch: int
    adopted_at: str
    source_version: str
    backup_path: str
    receipt_digest: str

    def __post_init__(self) -> None:
        if self.target_epoch != SUPPORTED_SCHEMA_EPOCH:
            raise MigrationSafetyError("only schema epoch 2 adoption is supported")
        if self.source_epoch is not None and self.source_epoch > self.target_epoch:
            raise FutureSchemaError("cannot adopt state from a future schema epoch")
        if not self.source_version.strip() or not self.receipt_digest:
            raise MigrationSafetyError("adoption requires source version and receipt digest")


@dataclass(frozen=True)
class BridgeCleanupDecision:
    """Explicit release decision; this object never performs cleanup."""

    allowed: bool
    reason: str
    required_epoch: int
    backup_verified: bool
    restore_proven: bool
    receipt_present: bool


def verify_backup_before_migration(
    backup_path: str | Path,
    source_files: Mapping[str, str | Path],
    verifier: BackupVerifier,
) -> BackupProof:
    """Verify a backup and capture source digests before destructive work.

    ``source_files`` is a name-to-path mapping for the live inputs.  Hashing
    those inputs after the adapter verifier runs catches a source changing
    between backup creation and migration preparation.  The function has no
    mutation authority and therefore remains safe to call during preflight.
    """
    backup = Path(backup_path).expanduser().resolve(strict=False)
    if not backup.is_dir():
        raise BackupVerificationError("backup path is not a directory")
    problems = tuple(verifier.verify(backup))
    if problems:
        raise BackupVerificationError("backup verification failed: " + "; ".join(map(str, problems)))
    digests = {}
    for name, raw_path in source_files.items():
        path = Path(raw_path).expanduser()
        if not path.is_file() or path.is_symlink():
            raise BackupVerificationError(f"source file is unavailable or indirect: {name}")
        digests[name] = _sha256(path)
    manifest = backup / "manifest.json"
    manifest_digest = _sha256(manifest) if manifest.is_file() else None
    return BackupProof(str(backup), _utc_now(), digests, manifest_digest)


def prove_restore(
    proof: BackupProof,
    restore_path: str | Path,
    restored_files: Mapping[str, str | Path],
) -> RestoreProof:
    """Independently hash restored files and compare them to backup coverage."""
    destination = Path(restore_path).expanduser().resolve(strict=False)
    if not destination.is_dir():
        raise RestoreVerificationError("restore path is not a directory")
    restored = {}
    for name, raw_path in restored_files.items():
        path = Path(raw_path).expanduser()
        if not path.is_file() or path.is_symlink():
            raise RestoreVerificationError(f"restored file is unavailable or indirect: {name}")
        if name not in proof.source_digests:
            raise RestoreVerificationError(f"restored file was not covered by backup: {name}")
        digest = _sha256(path)
        if digest != proof.source_digests[name]:
            raise RestoreVerificationError(f"restored file differs from backup: {name}")
        restored[name] = digest
    if set(restored) != set(proof.source_digests):
        missing = sorted(set(proof.source_digests) - set(restored))
        raise RestoreVerificationError("restore proof is incomplete: " + ", ".join(missing))
    return RestoreProof(proof.backup_path, str(destination), _utc_now(), restored)


def adopt_schema_epoch(
    *,
    source_epoch: int | None,
    target_epoch: int,
    source_version: str,
    backup_proof: BackupProof,
    receipt: Mapping[str, object],
) -> EpochAdoption:
    """Validate and record an epoch-2 adoption without changing storage."""
    if source_epoch is not None and source_epoch > SUPPORTED_SCHEMA_EPOCH:
        raise FutureSchemaError(
            f"schema epoch {source_epoch} is newer than supported epoch {SUPPORTED_SCHEMA_EPOCH}"
        )
    if target_epoch != SUPPORTED_SCHEMA_EPOCH:
        raise MigrationSafetyError("target schema epoch must be 2")
    if not backup_proof.complete:
        raise BackupVerificationError("epoch adoption requires a complete backup proof")
    if not isinstance(receipt, Mapping) or receipt.get("epoch") != target_epoch:
        raise MigrationSafetyError("adoption receipt does not prove the target epoch")
    canonical = json.dumps(dict(receipt), sort_keys=True, separators=(",", ":")).encode()
    return EpochAdoption(
        source_epoch, target_epoch, _utc_now(), source_version,
        backup_proof.backup_path, hashlib.sha256(canonical).hexdigest(),
    )


def decide_bridge_cleanup(
    *,
    adoption: EpochAdoption | None,
    backup_proof: BackupProof | None,
    restore_proof: RestoreProof | None,
    receipt_present: bool,
    bridge_tests_passed: bool,
) -> BridgeCleanupDecision:
    """Return whether temporary bridge code may be removed by a release step."""
    checks = (
        (adoption is not None, "epoch adoption is not proven"),
        (backup_proof is not None and backup_proof.complete, "backup is not verified"),
        (restore_proof is not None, "restore is not independently proven"),
        (receipt_present, "adoption receipt is missing"),
        (bridge_tests_passed, "bridge acceptance tests did not pass"),
    )
    for passed, reason in checks:
        if not passed:
            return BridgeCleanupDecision(False, reason, SUPPORTED_SCHEMA_EPOCH,
                                         backup_proof is not None and backup_proof.complete,
                                         restore_proof is not None, receipt_present)
    return BridgeCleanupDecision(True, "epoch adoption and independent restore proof complete",
                                 SUPPORTED_SCHEMA_EPOCH, True, True, True)


__all__ = [
    "BackupProof", "BackupVerificationError", "BackupVerifier", "BridgeCleanupDecision",
    "EpochAdoption", "FutureSchemaError", "MigrationSafetyError", "RestoreProof",
    "RestoreVerificationError", "SUPPORTED_SCHEMA_EPOCH", "adopt_schema_epoch",
    "decide_bridge_cleanup", "prove_restore", "verify_backup_before_migration",
]
