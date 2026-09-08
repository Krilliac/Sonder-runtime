"""Bounded local filesystem adapter for the offline recovery contract.

This adapter is intentionally a rehearsal adapter.  It restores an existing
Sonder backup into an operator-selected disposable directory and uses a
marker file to simulate a release upgrade.  It never reads or switches the
live release pointer, starts a service, contacts a provider, or participates
in failover.  ``workspace_root`` fences every disposable write and cleanup.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

import sonder_runtime.adapters.backup as backup_adapter
from sonder_runtime.application.updates.recovery_rehearsal import (
    ArtifactIntegrityError,
    BackupArtifact,
    BackupManifest,
    CleanupError,
    CleanupReceipt,
    MAX_CLEANUP_ENTRIES,
    RestoreReceipt,
    RevisionMismatchError,
    UpgradeAttempt,
)


_UPGRADE_MARKER = ".sonder-offline-rehearsal-release"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _indirect(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        checker = getattr(path, "is_junction", None)
        return bool(checker and checker())
    except OSError:
        return True


def _within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(root), str(candidate))) == str(root)
    except (OSError, ValueError):
        return False


class FilesystemOfflineRecoveryPort:
    """Use a fenced local directory to exercise restore and rollback paths."""

    def __init__(
        self,
        workspace_root: str | os.PathLike[str],
        *,
        upgrade_succeeds: bool = False,
    ) -> None:
        raw_root = Path(workspace_root).expanduser()
        if _indirect(raw_root):
            raise ValueError("workspace_root must be a regular directory")
        root = raw_root.resolve(strict=True)
        if _indirect(root) or not root.is_dir():
            raise ValueError("workspace_root must be a regular directory")
        if type(upgrade_succeeds) is not bool:
            raise ValueError("upgrade_succeeds must be boolean")
        self._root = root
        self._upgrade_succeeds = upgrade_succeeds
        self._owned: dict[Path, BackupManifest] = {}

    def inspect_backup(self, backup_ref: str) -> BackupManifest:
        source = self._backup_path(backup_ref)
        manifest_path = source / "manifest.json"
        if _indirect(manifest_path) or not manifest_path.is_file():
            raise ArtifactIntegrityError("backup manifest is missing")
        try:
            if manifest_path.stat().st_size > backup_adapter.MAX_MANIFEST_BYTES:
                raise ArtifactIntegrityError("backup manifest exceeds the size limit")
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ArtifactIntegrityError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise ArtifactIntegrityError(
                "backup manifest is unreadable: " + type(exc).__name__,
            ) from exc
        if not isinstance(raw, dict):
            raise ArtifactIntegrityError("backup manifest must be an object")
        if raw.get("format_version") != backup_adapter.MANIFEST_FORMAT_VERSION:
            raise ArtifactIntegrityError("unsupported backup manifest format")
        files = raw.get("files")
        if not isinstance(files, list) or not files:
            raise ArtifactIntegrityError("backup manifest lists no files")

        revision = raw.get("source_revision")
        if not isinstance(revision, str) or not revision.strip():
            revision = raw.get("commit_sha")
        if not isinstance(revision, str) or not revision.strip() or revision == "unknown":
            version = raw.get("application_version")
            if not isinstance(version, str) or not version.strip():
                version = "unknown"
            revision = "version:" + version.strip()

        artifacts: list[BackupArtifact] = []
        try:
            for entry in files:
                if not isinstance(entry, dict):
                    raise ValueError("file entry is not an object")
                path = entry["path"]
                store = Path(path).name.rsplit(".", 1)[0]
                artifacts.append(
                    BackupArtifact(
                        store=store,
                        relative_path=path,
                        size=entry["size"],
                        sha256=entry["sha256"],
                    )
                )
            artifacts.sort(key=lambda item: item.relative_path)
            checksum_path = source / "checksums.sha256"
            if _indirect(checksum_path) or not checksum_path.is_file():
                raise ValueError("checksum index is missing")
            manifest_digest = _sha256(manifest_path)
            checksum_digest = _sha256(checksum_path)
            return BackupManifest(
                backup_id=raw.get("backup_id") or source.name,
                source_revision=revision,
                manifest_sha256=manifest_digest,
                checksum_sha256=checksum_digest,
                artifacts=tuple(artifacts),
            )
        except ArtifactIntegrityError:
            raise
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ArtifactIntegrityError(
                "backup manifest contains invalid artifact metadata: "
                + type(exc).__name__,
            ) from exc

    def verify_backup(
        self, backup_ref: str, manifest: BackupManifest,
    ) -> tuple[str, ...]:
        try:
            current = self.inspect_backup(backup_ref)
        except Exception as exc:
            return ("manifest inspection failed: " + type(exc).__name__,)
        if current.digest != manifest.digest:
            return ("backup manifest changed during rehearsal",)
        problems = backup_adapter.verify_backup(self._backup_path(backup_ref))
        return tuple(str(problem) for problem in problems)

    def stage_restore(
        self, backup_ref: str, destination_ref: str, manifest: BackupManifest,
    ) -> str:
        destination = self._destination_path(destination_ref)
        backup_adapter.restore_to_empty(backup_ref, destination)
        self._owned[destination] = manifest
        return str(destination)

    def verify_restore(
        self, destination_ref: str, manifest: BackupManifest,
    ) -> RestoreReceipt:
        destination = self._owned_destination(destination_ref, manifest)
        return self._verify_members(destination, manifest, allow_marker=False)

    def apply_upgrade(
        self,
        destination_ref: str,
        *,
        source_revision: str,
        target_revision: str,
    ) -> UpgradeAttempt:
        destination = self._owned_destination(destination_ref)
        expected = self._owned[destination]
        if source_revision != expected.source_revision:
            raise RevisionMismatchError(
                "upgrade source revision does not match staged backup",
            )
        marker = destination / _UPGRADE_MARKER
        if _indirect(marker):
            raise ArtifactIntegrityError("upgrade marker is indirect")
        marker.write_text(target_revision, encoding="utf-8")
        reason = "offline scripted upgrade passed" if self._upgrade_succeeds else "offline scripted upgrade failed"
        return UpgradeAttempt(target_revision, self._upgrade_succeeds, reason)

    def verify_upgrade(self, destination_ref: str, target_revision: str) -> None:
        destination = self._owned_destination(destination_ref)
        marker = destination / _UPGRADE_MARKER
        try:
            actual = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ArtifactIntegrityError(
                "upgrade marker is unreadable: " + type(exc).__name__,
            ) from exc
        if actual != target_revision:
            raise ArtifactIntegrityError("upgrade marker revision mismatch")

    def rollback_upgrade(
        self,
        destination_ref: str,
        *,
        failed_revision: str,
        source_revision: str,
    ) -> None:
        destination = self._owned_destination(destination_ref)
        expected = self._owned[destination]
        if source_revision != expected.source_revision:
            raise RevisionMismatchError(
                "rollback source revision does not match staged backup",
            )
        marker = destination / _UPGRADE_MARKER
        try:
            actual = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ArtifactIntegrityError(
                "failed upgrade marker is unreadable: " + type(exc).__name__,
            ) from exc
        if actual != failed_revision:
            raise ArtifactIntegrityError("failed upgrade revision mismatch")
        marker.write_text(source_revision, encoding="utf-8")

    def restore_state(
        self, backup_ref: str, destination_ref: str, manifest: BackupManifest,
    ) -> RestoreReceipt:
        destination = self._owned_destination(destination_ref, manifest)
        current = self.inspect_backup(backup_ref)
        if current.digest != manifest.digest:
            raise ArtifactIntegrityError("backup changed before state restore")
        problems = self.verify_backup(backup_ref, manifest)
        if problems:
            raise ArtifactIntegrityError(
                "backup verification failed before state restore: "
                + "; ".join(problems[:8]),
            )
        source = self._backup_path(backup_ref)
        for item in manifest.artifacts:
            source_member = self._backup_member(source, item.relative_path)
            target = destination / Path(item.relative_path).name
            if _indirect(target) or not target.is_file():
                raise ArtifactIntegrityError(
                    "restore target is unavailable: " + item.relative_path,
                )
            shutil.copy2(source_member, target)
            if target.stat().st_size != item.size or _sha256(target) != item.sha256:
                raise ArtifactIntegrityError(
                    "restored artifact checksum mismatch: " + item.relative_path,
                )
        return self._verify_members(destination, manifest, allow_marker=True)

    def verify_rollback(
        self, destination_ref: str, manifest: BackupManifest, source_revision: str,
    ) -> None:
        destination = self._owned_destination(destination_ref, manifest)
        if source_revision != manifest.source_revision:
            raise RevisionMismatchError(
                "rollback verification revision does not match backup",
            )
        marker = destination / _UPGRADE_MARKER
        try:
            actual = marker.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ArtifactIntegrityError(
                "rollback marker is unreadable: " + type(exc).__name__,
            ) from exc
        if actual != source_revision:
            raise ArtifactIntegrityError("rollback did not restore source revision")
        self._verify_members(destination, manifest, allow_marker=True)

    def cleanup(self, destination_ref: str, *, max_entries: int) -> CleanupReceipt:
        if type(max_entries) is not int or not 1 <= max_entries <= MAX_CLEANUP_ENTRIES:
            raise CleanupError("cleanup bound is outside the allowed range")
        destination = self._owned_destination(destination_ref)
        if not destination.exists():
            # A missing path may have been moved while the rehearsal was
            # running.  Do not turn that uncertainty into a false cleanup
            # success; the owner must inspect and remove it explicitly.
            return CleanupReceipt(0, 1, 0, False)
        count, total, within_bound = self._scan(destination, max_entries)
        if not within_bound:
            return CleanupReceipt(0, count, total, False)
        try:
            shutil.rmtree(destination)
        except OSError as exc:
            raise CleanupError("disposable cleanup failed: " + type(exc).__name__) from exc
        self._owned.pop(destination, None)
        return CleanupReceipt(count, 0, total)

    def _backup_path(self, backup_ref: str) -> Path:
        raw_source = Path(backup_ref).expanduser()
        if _indirect(raw_source):
            raise ArtifactIntegrityError("backup reference is indirect")
        source = raw_source.resolve(strict=True)
        if _indirect(source) or not source.is_dir():
            raise ArtifactIntegrityError("backup reference is not a regular directory")
        return source

    def _destination_path(self, destination_ref: str) -> Path:
        raw = Path(destination_ref).expanduser()
        destination = raw.resolve(strict=False)
        if not _within(self._root, destination):
            raise CleanupError("disposable destination is outside workspace_root")
        if _indirect(raw):
            raise CleanupError("disposable destination is indirect")
        return destination

    def _owned_destination(
        self, destination_ref: str, manifest: BackupManifest | None = None,
    ) -> Path:
        destination = self._destination_path(destination_ref)
        expected = self._owned.get(destination)
        if expected is None:
            raise CleanupError("disposable destination is not owned by this rehearsal")
        if manifest is not None and expected.digest != manifest.digest:
            raise ArtifactIntegrityError("destination manifest identity changed")
        return destination

    def _backup_member(self, source: Path, relative_path: str) -> Path:
        member = source / relative_path
        if _indirect(member) or not member.is_file():
            raise ArtifactIntegrityError("backup artifact is unavailable: " + relative_path)
        resolved_source = source.resolve(strict=True)
        resolved_member = member.resolve(strict=True)
        if not _within(resolved_source, resolved_member):
            raise ArtifactIntegrityError("backup artifact escapes backup root")
        return resolved_member

    def _verify_members(
        self, destination: Path, manifest: BackupManifest, *, allow_marker: bool,
    ) -> RestoreReceipt:
        expected = {Path(item.relative_path).name for item in manifest.artifacts}
        allowed = expected | ({_UPGRADE_MARKER} if allow_marker else set())
        try:
            actual = {child.name for child in destination.iterdir()}
        except OSError as exc:
            raise ArtifactIntegrityError(
                "restore destination is unreadable: " + type(exc).__name__,
            ) from exc
        if actual - allowed:
            raise ArtifactIntegrityError("restore destination contains unlisted entries")
        if expected - actual:
            raise ArtifactIntegrityError("restore destination is missing artifacts")
        rows: list[tuple[str, str]] = []
        for item in manifest.artifacts:
            target = destination / Path(item.relative_path).name
            if _indirect(target) or not target.is_file():
                raise ArtifactIntegrityError("restored artifact is not a regular file")
            if target.stat().st_size != item.size or _sha256(target) != item.sha256:
                raise ArtifactIntegrityError(
                    "restored artifact checksum mismatch: " + item.relative_path,
                )
            rows.append((item.relative_path, item.sha256))
        return RestoreReceipt(str(destination), manifest.source_revision, tuple(rows))

    @staticmethod
    def _scan(path: Path, limit: int) -> tuple[int, int, bool]:
        pending = [path]
        count = 0
        total = 0
        while pending:
            current = pending.pop()
            try:
                children = tuple(current.iterdir())
            except OSError as exc:
                raise CleanupError("disposable tree is unreadable: " + type(exc).__name__) from exc
            for child in children:
                count += 1
                if count > limit:
                    return count, total, False
                if _indirect(child):
                    raise CleanupError("disposable tree contains an indirect entry")
                if child.is_dir():
                    pending.append(child)
                else:
                    try:
                        total += child.stat().st_size
                    except OSError as exc:
                        raise CleanupError("disposable entry is unreadable: " + type(exc).__name__) from exc
        return count, total, True


__all__ = ["FilesystemOfflineRecoveryPort"]
