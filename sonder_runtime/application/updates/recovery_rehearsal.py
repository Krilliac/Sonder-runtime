"""Provider-neutral offline backup/restore and upgrade rollback contract.

The application service in this module only sequences an injected local
adapter.  It does not open SQLite, start a release, switch a live pointer, or
contact a failover coordinator.  Adapters own those details and must expose
only a disposable rehearsal destination.  That keeps an offline recovery
drill useful on one PC while making it impossible for this contract to imply
live failover or multi-node recovery.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Protocol, Sequence


REHEARSAL_SCHEMA = "sonder.offline-recovery-rehearsal.v1"
MANIFEST_FORMAT_VERSION = 1
MAX_ARTIFACTS = 64
MAX_TOTAL_BYTES = 1 << 30
MAX_CLEANUP_ENTRIES = 256
MAX_REVISION_CHARS = 256
MAX_REFERENCE_CHARS = 512
MAX_REASON_CHARS = 256


class RehearsalError(RuntimeError):
    """The offline recovery rehearsal could not establish its evidence."""

    def __init__(self, message: str, *, steps: Sequence["RehearsalStep"] = ()) -> None:
        super().__init__(message)
        self.steps = tuple(steps)


class RevisionMismatchError(RehearsalError):
    """The selected backup belongs to a different source revision."""


class ArtifactIntegrityError(RehearsalError):
    """The manifest, checksum index, or restored artifact did not verify."""


class CleanupError(RehearsalError):
    """The disposable rehearsal tree was not cleaned within its bound."""


class RehearsalStep(str, Enum):
    INSPECT_BACKUP = "inspect_backup"
    VERIFY_MANIFEST = "verify_manifest"
    VERIFY_ARTIFACTS = "verify_artifacts"
    STAGE_RESTORE = "stage_restore"
    VERIFY_RESTORE = "verify_restore"
    APPLY_UPGRADE = "apply_upgrade"
    VERIFY_UPGRADE = "verify_upgrade"
    ROLLBACK_UPGRADE = "rollback_upgrade"
    RESTORE_STATE = "restore_state"
    VERIFY_ROLLBACK = "verify_rollback"
    CLEANUP = "cleanup"


def _text(value: object, field: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(char in value for char in "\r\n\x00")
    ):
        raise ValueError(f"{field} must be bounded non-empty text")
    return value.strip()


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field} must be a SHA-256 digest")
    lowered = value.lower()
    if any(char not in "0123456789abcdef" for char in lowered):
        raise ValueError(f"{field} must be a SHA-256 digest")
    return lowered


def _safe_state_path(value: object) -> str:
    path = _text(value, "artifact relative_path", 256)
    parts = path.split("/")
    if (
        "\\" in path
        or len(parts) != 2
        or parts[0] != "state"
        or not parts[1]
        or parts[1] in {".", ".."}
    ):
        raise ValueError("artifact relative_path must be a direct state member")
    return path


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    """One manifest-covered authoritative state member."""

    store: str
    relative_path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "store", _text(self.store, "artifact store", 128))
        object.__setattr__(self, "relative_path", _safe_state_path(self.relative_path))
        if type(self.size) is not int or self.size < 0:
            raise ValueError("artifact size must be a non-negative integer")
        object.__setattr__(self, "sha256", _digest(self.sha256, "artifact sha256"))

    def as_dict(self) -> dict[str, object]:
        return {
            "store": self.store,
            "path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Immutable identity and checksum projection of a backup directory."""

    backup_id: str
    source_revision: str
    manifest_sha256: str
    checksum_sha256: str
    artifacts: tuple[BackupArtifact, ...]
    format_version: int = MANIFEST_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "backup_id", _text(self.backup_id, "backup_id", 256))
        object.__setattr__(
            self, "source_revision", _text(
                self.source_revision, "source_revision", MAX_REVISION_CHARS,
            ),
        )
        object.__setattr__(
            self, "manifest_sha256", _digest(self.manifest_sha256, "manifest_sha256"),
        )
        object.__setattr__(
            self, "checksum_sha256", _digest(self.checksum_sha256, "checksum_sha256"),
        )
        if type(self.format_version) is not int or self.format_version != MANIFEST_FORMAT_VERSION:
            raise ValueError("unsupported offline rehearsal manifest version")
        if not isinstance(self.artifacts, tuple) or not self.artifacts:
            raise ValueError("backup manifest must contain artifacts")
        if len(self.artifacts) > MAX_ARTIFACTS:
            raise ValueError("backup manifest contains too many artifacts")
        if any(not isinstance(item, BackupArtifact) for item in self.artifacts):
            raise ValueError("backup manifest artifacts are malformed")
        paths = tuple(item.relative_path for item in self.artifacts)
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("backup artifacts must be unique and sorted by path")
        if sum(item.size for item in self.artifacts) > MAX_TOTAL_BYTES:
            raise ValueError("backup manifest exceeds the byte bound")

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.artifacts)

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": REHEARSAL_SCHEMA,
            "format_version": self.format_version,
            "backup_id": self.backup_id,
            "source_revision": self.source_revision,
            "manifest_sha256": self.manifest_sha256,
            "checksum_sha256": self.checksum_sha256,
            "artifacts": [item.as_dict() for item in self.artifacts],
        }


@dataclass(frozen=True, slots=True)
class RestoreReceipt:
    """Digest evidence for every member restored into a disposable tree."""

    destination_ref: str
    source_revision: str
    artifacts: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "destination_ref", _text(
                self.destination_ref, "destination_ref", MAX_REFERENCE_CHARS,
            ),
        )
        object.__setattr__(
            self, "source_revision", _text(
                self.source_revision, "restore source_revision", MAX_REVISION_CHARS,
            ),
        )
        if not isinstance(self.artifacts, tuple) or not self.artifacts:
            raise ValueError("restore receipt must contain artifacts")
        normalised: list[tuple[str, str]] = []
        for path, digest in self.artifacts:
            normalised.append((_safe_state_path(path), _digest(digest, "restored artifact digest")))
        paths = tuple(path for path, _ in normalised)
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("restore receipt artifacts must be unique and sorted")
        object.__setattr__(self, "artifacts", tuple(normalised))

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical({
            "destination_ref": self.destination_ref,
            "source_revision": self.source_revision,
            "artifacts": self.artifacts,
        })).hexdigest()


@dataclass(frozen=True, slots=True)
class UpgradeAttempt:
    """The local adapter's bounded result for a candidate upgrade."""

    target_revision: str
    succeeded: bool
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_revision", _text(
                self.target_revision, "upgrade target_revision", MAX_REVISION_CHARS,
            ),
        )
        if type(self.succeeded) is not bool:
            raise ValueError("upgrade succeeded must be boolean")
        object.__setattr__(self, "reason", _text(self.reason, "upgrade reason", MAX_REASON_CHARS) if self.reason else "")


@dataclass(frozen=True, slots=True)
class CleanupReceipt:
    """Bounded cleanup evidence returned by the disposable adapter."""

    removed_entries: int
    remaining_entries: int
    removed_bytes: int
    bounded: bool = True

    def __post_init__(self) -> None:
        for value, field in (
            (self.removed_entries, "removed_entries"),
            (self.remaining_entries, "remaining_entries"),
            (self.removed_bytes, "removed_bytes"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        if type(self.bounded) is not bool:
            raise ValueError("cleanup bounded flag must be boolean")

    @property
    def complete(self) -> bool:
        return self.bounded and self.remaining_entries == 0


@dataclass(frozen=True, slots=True)
class OfflineRehearsalRequest:
    """Inputs to one local-only, disposable recovery drill."""

    backup_ref: str
    destination_ref: str
    source_revision: str
    target_revision: str
    expect_upgrade_failure: bool = True
    max_artifacts: int = MAX_ARTIFACTS
    max_bytes: int = MAX_TOTAL_BYTES
    max_cleanup_entries: int = MAX_CLEANUP_ENTRIES

    def __post_init__(self) -> None:
        object.__setattr__(self, "backup_ref", _text(self.backup_ref, "backup_ref", MAX_REFERENCE_CHARS))
        object.__setattr__(self, "destination_ref", _text(self.destination_ref, "destination_ref", MAX_REFERENCE_CHARS))
        object.__setattr__(self, "source_revision", _text(self.source_revision, "source_revision", MAX_REVISION_CHARS))
        object.__setattr__(self, "target_revision", _text(self.target_revision, "target_revision", MAX_REVISION_CHARS))
        if self.source_revision == self.target_revision:
            raise ValueError("source and target revisions must differ")
        if type(self.expect_upgrade_failure) is not bool:
            raise ValueError("expect_upgrade_failure must be boolean")
        if type(self.max_artifacts) is not int or not 1 <= self.max_artifacts <= MAX_ARTIFACTS:
            raise ValueError("max_artifacts is outside the bound")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= MAX_TOTAL_BYTES:
            raise ValueError("max_bytes is outside the bound")
        if type(self.max_cleanup_entries) is not int or not 1 <= self.max_cleanup_entries <= MAX_CLEANUP_ENTRIES:
            raise ValueError("max_cleanup_entries is outside the bound")


class OfflineRecoveryPort(Protocol):
    """Adapter port for a disposable local backup and release rehearsal."""

    def inspect_backup(self, backup_ref: str) -> BackupManifest: ...

    def verify_backup(
        self, backup_ref: str, manifest: BackupManifest,
    ) -> Sequence[str]: ...

    def stage_restore(
        self, backup_ref: str, destination_ref: str, manifest: BackupManifest,
    ) -> str: ...

    def verify_restore(
        self, destination_ref: str, manifest: BackupManifest,
    ) -> RestoreReceipt: ...

    def apply_upgrade(
        self, destination_ref: str, *, source_revision: str, target_revision: str,
    ) -> UpgradeAttempt: ...

    def verify_upgrade(self, destination_ref: str, target_revision: str) -> None: ...

    def rollback_upgrade(
        self, destination_ref: str, *, failed_revision: str, source_revision: str,
    ) -> None: ...

    def restore_state(
        self, backup_ref: str, destination_ref: str, manifest: BackupManifest,
    ) -> RestoreReceipt: ...

    def verify_rollback(
        self, destination_ref: str, manifest: BackupManifest, source_revision: str,
    ) -> None: ...

    def cleanup(self, destination_ref: str, *, max_entries: int) -> CleanupReceipt: ...


@dataclass(frozen=True, slots=True)
class OfflineRehearsalReport:
    """Immutable evidence that one offline drill completed."""

    request: OfflineRehearsalRequest
    manifest: BackupManifest
    staged_ref: str
    restore: RestoreReceipt
    upgrade: UpgradeAttempt
    rollback_verified: bool
    cleanup: CleanupReceipt
    steps: tuple[RehearsalStep, ...]
    schema: str = REHEARSAL_SCHEMA
    live_failover: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.request, OfflineRehearsalRequest):
            raise ValueError("rehearsal report request is malformed")
        if not isinstance(self.manifest, BackupManifest):
            raise ValueError("rehearsal report manifest is malformed")
        if (
            not isinstance(self.staged_ref, str)
            or not self.staged_ref.strip()
            or len(self.staged_ref) > MAX_REFERENCE_CHARS
            or any(char in self.staged_ref for char in "\r\n\x00")
        ):
            raise ValueError("rehearsal report staged_ref is malformed")
        if not isinstance(self.restore, RestoreReceipt):
            raise ValueError("rehearsal report restore evidence is malformed")
        if not isinstance(self.upgrade, UpgradeAttempt):
            raise ValueError("rehearsal report upgrade evidence is malformed")
        if not isinstance(self.rollback_verified, bool):
            raise ValueError("rehearsal report rollback flag is malformed")
        if not isinstance(self.cleanup, CleanupReceipt):
            raise ValueError("rehearsal report cleanup evidence is malformed")
        if self.schema != REHEARSAL_SCHEMA:
            raise ValueError("unsupported rehearsal report schema")
        if self.live_failover is not False:
            raise ValueError("offline rehearsal cannot claim live failover")
        if not isinstance(self.steps, tuple) or not self.steps:
            raise ValueError("rehearsal report must include steps")
        if any(not isinstance(step, RehearsalStep) for step in self.steps):
            raise ValueError("rehearsal report steps are malformed")
        if self.steps[-1] is not RehearsalStep.CLEANUP:
            raise ValueError("rehearsal report must end with cleanup")
        if not self.cleanup.complete:
            raise ValueError("rehearsal report requires complete cleanup")
        if self.manifest.source_revision != self.request.source_revision:
            raise ValueError("rehearsal report source revision is inconsistent")
        if self.restore.destination_ref != self.staged_ref:
            raise ValueError("rehearsal report restore destination is inconsistent")
        if self.restore.source_revision != self.manifest.source_revision:
            raise ValueError("rehearsal report restore revision is inconsistent")
        if self.restore.artifacts != _expected_artifacts(self.manifest):
            raise ValueError("rehearsal report restore digests are inconsistent")
        if self.upgrade.target_revision != self.request.target_revision:
            raise ValueError("rehearsal report upgrade revision is inconsistent")
        if self.request.expect_upgrade_failure:
            if self.upgrade.succeeded or not self.rollback_verified:
                raise ValueError("rehearsal report rollback evidence is incomplete")
            expected_steps = (
                RehearsalStep.INSPECT_BACKUP,
                RehearsalStep.VERIFY_MANIFEST,
                RehearsalStep.VERIFY_ARTIFACTS,
                RehearsalStep.STAGE_RESTORE,
                RehearsalStep.VERIFY_RESTORE,
                RehearsalStep.APPLY_UPGRADE,
                RehearsalStep.ROLLBACK_UPGRADE,
                RehearsalStep.RESTORE_STATE,
                RehearsalStep.VERIFY_ROLLBACK,
                RehearsalStep.CLEANUP,
            )
        elif self.rollback_verified or not self.upgrade.succeeded:
            raise ValueError("rehearsal report successful-upgrade evidence is incomplete")
        else:
            expected_steps = (
                RehearsalStep.INSPECT_BACKUP,
                RehearsalStep.VERIFY_MANIFEST,
                RehearsalStep.VERIFY_ARTIFACTS,
                RehearsalStep.STAGE_RESTORE,
                RehearsalStep.VERIFY_RESTORE,
                RehearsalStep.APPLY_UPGRADE,
                RehearsalStep.VERIFY_UPGRADE,
                RehearsalStep.CLEANUP,
            )
        if self.steps != expected_steps:
            raise ValueError("rehearsal report steps are out of order or incomplete")
        if self.cleanup.removed_entries > self.request.max_cleanup_entries:
            raise ValueError("rehearsal report cleanup exceeds its bound")

    @property
    def evidence_digest(self) -> str:
        return hashlib.sha256(_canonical(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "live_failover": self.live_failover,
            "backup_id": self.manifest.backup_id,
            "source_revision": self.manifest.source_revision,
            "target_revision": self.request.target_revision,
            "manifest_sha256": self.manifest.manifest_sha256,
            "checksum_sha256": self.manifest.checksum_sha256,
            "manifest_digest": self.manifest.digest,
            "restore_digest": self.restore.digest,
            "upgrade_succeeded": self.upgrade.succeeded,
            "rollback_verified": self.rollback_verified,
            "cleanup": {
                "removed_entries": self.cleanup.removed_entries,
                "remaining_entries": self.cleanup.remaining_entries,
                "removed_bytes": self.cleanup.removed_bytes,
                "bounded": self.cleanup.bounded,
            },
            "steps": [step.value for step in self.steps],
        }


def _expected_artifacts(manifest: BackupManifest) -> tuple[tuple[str, str], ...]:
    return tuple((item.relative_path, item.sha256) for item in manifest.artifacts)


def _validate_restore(
    receipt: RestoreReceipt,
    manifest: BackupManifest,
    steps: Sequence[RehearsalStep],
    *,
    destination_ref: str,
) -> None:
    if receipt.destination_ref != destination_ref:
        raise ArtifactIntegrityError(
            "restored state destination does not match staged destination", steps=steps,
        )
    if receipt.source_revision != manifest.source_revision:
        raise ArtifactIntegrityError(
            "restored state revision does not match backup revision", steps=steps,
        )
    if receipt.artifacts != _expected_artifacts(manifest):
        raise ArtifactIntegrityError(
            "restored artifact digest set does not match backup manifest", steps=steps,
        )


class OfflineRecoveryRehearsal:
    """Sequence and validate one bounded offline restore/rollback rehearsal."""

    def __init__(self, port: OfflineRecoveryPort) -> None:
        required = (
            "inspect_backup", "verify_backup", "stage_restore", "verify_restore",
            "apply_upgrade", "verify_upgrade", "rollback_upgrade", "restore_state",
            "verify_rollback", "cleanup",
        )
        missing = [name for name in required if not callable(getattr(port, name, None))]
        if missing:
            raise TypeError("offline recovery port lacks: " + ", ".join(missing))
        self._port = port

    def run(self, request: OfflineRehearsalRequest) -> OfflineRehearsalReport:
        if not isinstance(request, OfflineRehearsalRequest):
            raise TypeError("request must be an OfflineRehearsalRequest")
        steps: list[RehearsalStep] = [RehearsalStep.INSPECT_BACKUP]
        manifest = self._inspect(request, steps)
        if manifest.source_revision != request.source_revision:
            raise RevisionMismatchError(
                "backup source revision does not match requested revision", steps=steps,
            )
        if len(manifest.artifacts) > request.max_artifacts or manifest.total_bytes > request.max_bytes:
            raise ArtifactIntegrityError(
                "backup manifest exceeds the rehearsal bound", steps=steps,
            )
        steps.append(RehearsalStep.VERIFY_MANIFEST)

        staged_ref: str | None = None
        cleanup: CleanupReceipt | None = None
        pending: Exception | None = None
        restore: RestoreReceipt | None = None
        upgrade: UpgradeAttempt | None = None
        rollback_verified = False
        try:
            problems = self._port.verify_backup(request.backup_ref, manifest)
            if problems:
                raise ArtifactIntegrityError(
                    "backup verification failed: " + "; ".join(str(item) for item in problems[:8]),
                    steps=steps,
                )
            steps.append(RehearsalStep.VERIFY_ARTIFACTS)

            staged_ref = self._port.stage_restore(
                request.backup_ref, request.destination_ref, manifest,
            )
            if not isinstance(staged_ref, str) or not staged_ref.strip():
                raise RehearsalError("stage_restore returned an empty reference", steps=steps)
            steps.append(RehearsalStep.STAGE_RESTORE)

            restore = self._port.verify_restore(staged_ref, manifest)
            if not isinstance(restore, RestoreReceipt):
                raise ArtifactIntegrityError("restore verification returned malformed evidence", steps=steps)
            _validate_restore(
                restore, manifest, steps, destination_ref=staged_ref,
            )
            steps.append(RehearsalStep.VERIFY_RESTORE)

            upgrade = self._port.apply_upgrade(
                staged_ref,
                source_revision=manifest.source_revision,
                target_revision=request.target_revision,
            )
            if not isinstance(upgrade, UpgradeAttempt) or upgrade.target_revision != request.target_revision:
                raise RehearsalError("upgrade adapter returned an invalid attempt", steps=steps)
            steps.append(RehearsalStep.APPLY_UPGRADE)

            if upgrade.succeeded:
                if request.expect_upgrade_failure:
                    raise RehearsalError(
                        "upgrade unexpectedly succeeded; rollback boundary was not exercised",
                        steps=steps,
                    )
                self._port.verify_upgrade(staged_ref, request.target_revision)
                steps.append(RehearsalStep.VERIFY_UPGRADE)
            else:
                if not request.expect_upgrade_failure:
                    raise RehearsalError(
                        "upgrade failed while successful-upgrade mode was requested",
                        steps=steps,
                    )
                self._port.rollback_upgrade(
                    staged_ref,
                    failed_revision=request.target_revision,
                    source_revision=manifest.source_revision,
                )
                steps.append(RehearsalStep.ROLLBACK_UPGRADE)
                restored = self._port.restore_state(
                    request.backup_ref, staged_ref, manifest,
                )
                if not isinstance(restored, RestoreReceipt):
                    raise ArtifactIntegrityError("state restore returned malformed evidence", steps=steps)
                _validate_restore(
                    restored, manifest, steps, destination_ref=staged_ref,
                )
                steps.append(RehearsalStep.RESTORE_STATE)
                self._port.verify_rollback(
                    staged_ref, manifest, manifest.source_revision,
                )
                steps.append(RehearsalStep.VERIFY_ROLLBACK)
                rollback_verified = True
                restore = restored
        except Exception as exc:
            pending = exc
        finally:
            if staged_ref is not None:
                steps.append(RehearsalStep.CLEANUP)
                try:
                    cleanup = self._port.cleanup(
                        staged_ref, max_entries=request.max_cleanup_entries,
                    )
                    if not isinstance(cleanup, CleanupReceipt):
                        raise CleanupError("cleanup returned malformed evidence", steps=steps)
                    if cleanup.removed_entries > request.max_cleanup_entries or not cleanup.complete:
                        raise CleanupError(
                            "cleanup did not complete within the rehearsal bound", steps=steps,
                        )
                except Exception as cleanup_error:
                    if pending is None:
                        pending = cleanup_error

        if pending is not None:
            if isinstance(pending, RehearsalError):
                pending.steps = tuple(steps)
                raise pending
            raise RehearsalError(
                "offline rehearsal failed: " + type(pending).__name__, steps=steps,
            ) from pending
        if manifest is None or staged_ref is None or restore is None or upgrade is None or cleanup is None:
            raise RehearsalError("offline rehearsal did not produce complete evidence", steps=steps)
        return OfflineRehearsalReport(
            request=request,
            manifest=manifest,
            staged_ref=staged_ref,
            restore=restore,
            upgrade=upgrade,
            rollback_verified=rollback_verified,
            cleanup=cleanup,
            steps=tuple(steps),
        )

    def _inspect(
        self, request: OfflineRehearsalRequest, steps: Sequence[RehearsalStep],
    ) -> BackupManifest:
        try:
            manifest = self._port.inspect_backup(request.backup_ref)
        except Exception as exc:
            raise ArtifactIntegrityError(
                "backup manifest could not be inspected: " + type(exc).__name__, steps=steps,
            ) from exc
        if not isinstance(manifest, BackupManifest):
            raise ArtifactIntegrityError("backup adapter returned a malformed manifest", steps=steps)
        return manifest


__all__ = [
    "ArtifactIntegrityError", "BackupArtifact", "BackupManifest", "CleanupError",
    "CleanupReceipt", "MANIFEST_FORMAT_VERSION", "MAX_ARTIFACTS", "MAX_CLEANUP_ENTRIES",
    "MAX_TOTAL_BYTES", "OfflineRecoveryPort", "OfflineRecoveryRehearsal",
    "OfflineRehearsalReport", "OfflineRehearsalRequest", "REHEARSAL_SCHEMA",
    "RehearsalError", "RehearsalStep", "RestoreReceipt", "RevisionMismatchError",
    "UpgradeAttempt",
]
