from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import shutil

import pytest

from sonder_runtime.application.updates.recovery_rehearsal import (
    ArtifactIntegrityError,
    BackupArtifact,
    BackupManifest,
    CleanupError,
    CleanupReceipt,
    OfflineRecoveryRehearsal,
    OfflineRehearsalRequest,
    RehearsalStep,
    RestoreReceipt,
    RevisionMismatchError,
    UpgradeAttempt,
)
from sonder_runtime.adapters.updates.offline_rehearsal import (
    FilesystemOfflineRecoveryPort,
)


def _digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def _manifest(*, revision: str = "rev-a") -> BackupManifest:
    return BackupManifest(
        backup_id="backup-1",
        source_revision=revision,
        manifest_sha256=_digest(b"manifest"),
        checksum_sha256=_digest(b"checksums"),
        artifacts=(
            BackupArtifact(
                store="memory",
                relative_path="state/memory.db",
                size=3,
                sha256=_digest(b"one"),
            ),
            BackupArtifact(
                store="operations",
                relative_path="state/operations.db",
                size=3,
                sha256=_digest(b"two"),
            ),
        ),
    )


@dataclass
class FakeRecoveryPort:
    manifest: BackupManifest
    upgrade_succeeds: bool = False
    cleanup_result: CleanupReceipt = CleanupReceipt(2, 0, 6)

    def __post_init__(self) -> None:
        self.calls: list[tuple] = []

    def inspect_backup(self, backup_ref: str) -> BackupManifest:
        self.calls.append(("inspect", backup_ref))
        return self.manifest

    def verify_backup(self, backup_ref: str, manifest: BackupManifest) -> tuple[str, ...]:
        self.calls.append(("verify_backup", backup_ref, manifest.digest))
        return ()

    def stage_restore(
        self, backup_ref: str, destination_ref: str, manifest: BackupManifest
    ) -> str:
        self.calls.append(("stage_restore", backup_ref, destination_ref))
        return "stage-1"

    def verify_restore(
        self, destination_ref: str, manifest: BackupManifest
    ) -> RestoreReceipt:
        self.calls.append(("verify_restore", destination_ref))
        return RestoreReceipt(
            destination_ref,
            manifest.source_revision,
            tuple((item.relative_path, item.sha256) for item in manifest.artifacts),
        )

    def apply_upgrade(
        self,
        destination_ref: str,
        *,
        source_revision: str,
        target_revision: str,
    ) -> UpgradeAttempt:
        self.calls.append(("apply_upgrade", destination_ref, target_revision))
        return UpgradeAttempt(target_revision, self.upgrade_succeeds, "scripted")

    def verify_upgrade(self, destination_ref: str, target_revision: str) -> None:
        self.calls.append(("verify_upgrade", destination_ref, target_revision))

    def rollback_upgrade(
        self,
        destination_ref: str,
        *,
        failed_revision: str,
        source_revision: str,
    ) -> None:
        self.calls.append(("rollback_upgrade", destination_ref, source_revision))

    def restore_state(
        self,
        backup_ref: str,
        destination_ref: str,
        manifest: BackupManifest,
    ) -> RestoreReceipt:
        self.calls.append(("restore_state", backup_ref, destination_ref))
        return RestoreReceipt(
            destination_ref,
            manifest.source_revision,
            tuple((item.relative_path, item.sha256) for item in manifest.artifacts),
        )

    def verify_rollback(
        self,
        destination_ref: str,
        manifest: BackupManifest,
        source_revision: str,
    ) -> None:
        self.calls.append(("verify_rollback", destination_ref, source_revision))

    def cleanup(self, destination_ref: str, *, max_entries: int) -> CleanupReceipt:
        self.calls.append(("cleanup", destination_ref, max_entries))
        return self.cleanup_result


def _request(**overrides) -> OfflineRehearsalRequest:
    values = {
        "backup_ref": "backup://local/backup-1",
        "destination_ref": "tmp/rehearsal-1",
        "source_revision": "rev-a",
        "target_revision": "rev-b",
    }
    values.update(overrides)
    return OfflineRehearsalRequest(**values)


def test_rehearsal_records_ordered_restore_and_rollback_steps():
    port = FakeRecoveryPort(_manifest())

    report = OfflineRecoveryRehearsal(port).run(_request())

    assert report.live_failover is False
    assert report.rollback_verified is True
    assert report.cleanup.complete is True
    assert report.steps == (
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
    assert [call[0] for call in port.calls] == [
        "inspect",
        "verify_backup",
        "stage_restore",
        "verify_restore",
        "apply_upgrade",
        "rollback_upgrade",
        "restore_state",
        "verify_rollback",
        "cleanup",
    ]


def test_revision_mismatch_refuses_before_any_mutation():
    port = FakeRecoveryPort(_manifest(revision="rev-a"))

    with pytest.raises(RevisionMismatchError, match="revision"):
        OfflineRecoveryRehearsal(port).run(_request(source_revision="rev-other"))

    assert [call[0] for call in port.calls] == ["inspect"]


def test_corrupt_artifact_refuses_before_staging():
    class CorruptPort(FakeRecoveryPort):
        def verify_backup(self, backup_ref, manifest):
            self.calls.append(("verify_backup", backup_ref, manifest.digest))
            return ("state/memory.db: checksum mismatch",)

    port = CorruptPort(_manifest())

    with pytest.raises(ArtifactIntegrityError, match="checksum mismatch"):
        OfflineRecoveryRehearsal(port).run(_request())

    assert [call[0] for call in port.calls] == ["inspect", "verify_backup"]


def test_restore_revision_and_artifact_digest_are_checked_before_upgrade():
    class MismatchedRestorePort(FakeRecoveryPort):
        def verify_restore(self, destination_ref, manifest):
            self.calls.append(("verify_restore", destination_ref))
            return RestoreReceipt(
                destination_ref,
                "wrong-revision",
                tuple((item.relative_path, "0" * 64) for item in manifest.artifacts),
            )

    port = MismatchedRestorePort(_manifest())

    with pytest.raises(ArtifactIntegrityError, match="restore"):
        OfflineRecoveryRehearsal(port).run(_request())

    assert [call[0] for call in port.calls] == [
        "inspect",
        "verify_backup",
        "stage_restore",
        "verify_restore",
        "cleanup",
    ]


def test_restore_destination_binding_is_checked_before_upgrade():
    class WrongDestinationPort(FakeRecoveryPort):
        def verify_restore(self, destination_ref, manifest):
            self.calls.append(("verify_restore", destination_ref))
            return RestoreReceipt(
                "another-stage",
                manifest.source_revision,
                tuple((item.relative_path, item.sha256) for item in manifest.artifacts),
            )

    port = WrongDestinationPort(_manifest())

    with pytest.raises(ArtifactIntegrityError, match="destination"):
        OfflineRecoveryRehearsal(port).run(_request())

    assert [call[0] for call in port.calls] == [
        "inspect",
        "verify_backup",
        "stage_restore",
        "verify_restore",
        "cleanup",
    ]


def test_cleanup_must_be_complete_and_within_bound():
    port = FakeRecoveryPort(_manifest(), cleanup_result=CleanupReceipt(1, 1, 3))

    with pytest.raises(CleanupError, match="cleanup"):
        OfflineRecoveryRehearsal(port).run(_request())

    assert port.calls[-1][0] == "cleanup"


def test_successful_upgrade_can_be_rehearsed_without_rollback():
    port = FakeRecoveryPort(_manifest(), upgrade_succeeds=True)

    report = OfflineRecoveryRehearsal(port).run(
        _request(expect_upgrade_failure=False)
    )

    assert report.rollback_verified is False
    assert RehearsalStep.VERIFY_UPGRADE in report.steps
    assert RehearsalStep.ROLLBACK_UPGRADE not in report.steps
    assert [call[0] for call in port.calls] == [
        "inspect",
        "verify_backup",
        "stage_restore",
        "verify_restore",
        "apply_upgrade",
        "verify_upgrade",
        "cleanup",
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"backup_id": "", "source_revision": "rev-a"},
        {"backup_id": "backup-1", "source_revision": ""},
    ],
)
def test_manifest_requires_identity(kwargs):
    values = {
        "backup_id": "backup-1",
        "source_revision": "rev-a",
        "manifest_sha256": _digest(b"manifest"),
        "checksum_sha256": _digest(b"checksums"),
        "artifacts": (),
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        BackupManifest(**values)


def _filesystem_backup(root: Path, *, revision: str = "rev-a") -> Path:
    backup = root / "backup"
    state = backup / "state"
    state.mkdir(parents=True)
    database = state / "memory.db"
    conn = sqlite3.connect(str(database))
    try:
        conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO facts (body) VALUES ('offline')")
        conn.commit()
    finally:
        conn.close()
    content = database.read_bytes()
    digest = _digest(content)
    from sonder_runtime.adapters.persistence import migrations

    status = migrations.status_read_only("memory", str(database))
    manifest = {
        "format_version": 1,
        "backup_id": "backup-1",
        "source_revision": revision,
        "application_version": "test",
        "commit_sha": revision,
        "schema_versions": {
            "memory": {
                "applied": list(status.applied),
                "pending": list(status.pending),
            }
        },
        "files": [{
            "path": "state/memory.db", "size": len(content), "sha256": digest,
        }],
    }
    manifest_path = backup / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (backup / "checksums.sha256").write_text(
        f"{digest}  state/memory.db\n"
        f"{_digest(manifest_path.read_bytes())}  manifest.json\n",
        encoding="utf-8",
    )
    return backup


def _filesystem_request(backup: Path, stage: Path, **overrides):
    values = {
        "backup_ref": str(backup),
        "destination_ref": str(stage),
        "source_revision": "rev-a",
        "target_revision": "rev-b",
    }
    values.update(overrides)
    return OfflineRehearsalRequest(**values)


def test_filesystem_adapter_rehearses_real_manifest_and_sqlite_restore(tmp_path):
    backup = _filesystem_backup(tmp_path)
    stage = tmp_path / "stage"
    port = FilesystemOfflineRecoveryPort(tmp_path)

    report = OfflineRecoveryRehearsal(port).run(
        _filesystem_request(backup, stage)
    )

    assert report.manifest.source_revision == "rev-a"
    assert report.manifest.manifest_sha256 == _digest(
        (backup / "manifest.json").read_bytes()
    )
    assert report.rollback_verified
    assert report.cleanup.complete
    assert not stage.exists()


def test_filesystem_adapter_refuses_corrupt_artifact_before_destination(tmp_path):
    backup = _filesystem_backup(tmp_path)
    member = backup / "state" / "memory.db"
    member.write_bytes(member.read_bytes() + b"corrupt")
    stage = tmp_path / "stage"

    with pytest.raises(ArtifactIntegrityError, match="checksum"):
        OfflineRecoveryRehearsal(FilesystemOfflineRecoveryPort(tmp_path)).run(
            _filesystem_request(backup, stage)
        )

    assert not stage.exists()


def test_filesystem_adapter_refuses_revision_mismatch_before_destination(tmp_path):
    backup = _filesystem_backup(tmp_path, revision="rev-a")
    stage = tmp_path / "stage"

    with pytest.raises(RevisionMismatchError, match="revision"):
        OfflineRecoveryRehearsal(FilesystemOfflineRecoveryPort(tmp_path)).run(
            _filesystem_request(backup, stage, source_revision="rev-old")
        )

    assert not stage.exists()


def test_filesystem_cleanup_stops_at_bound_and_leaves_owned_tree(tmp_path):
    backup = _filesystem_backup(tmp_path)
    stage = tmp_path / "stage"
    port = FilesystemOfflineRecoveryPort(tmp_path)

    with pytest.raises(CleanupError, match="cleanup"):
        OfflineRecoveryRehearsal(port).run(
            _filesystem_request(backup, stage, max_cleanup_entries=1)
        )

    assert stage.is_dir()
    shutil.rmtree(stage)
