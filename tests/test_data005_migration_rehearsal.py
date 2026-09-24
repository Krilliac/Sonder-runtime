"""DATA-005 real-file/process migration and restore evidence."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sonder_runtime.adapters.persistence.migration_rehearsal import (
    rehearse_bridge_migration,
)
from sonder_runtime.application.persistence.migration_safety import (
    BackupProof,
    BackupVerificationError,
    prove_restore,
    verify_backup_before_migration,
)

DB_NAMES = ("memory.db", "autopilot.db", "fleet.db", "operations.db", "updates.db")


def _legacy_home(path: Path) -> None:
    for name in DB_NAMES:
        connection = sqlite3.connect(path / name)
        connection.execute("CREATE TABLE seed (value TEXT NOT NULL)")
        connection.execute("INSERT INTO seed VALUES (?)", (name,))
        connection.commit()
        connection.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_real_sqlite_rehearsal_proves_backup_restore_crash_resume_and_epoch_ownership(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _legacy_home(source)

    report = rehearse_bridge_migration(source, failure_boundary="after_data_adoption")

    assert report.backup_verified
    assert report.restore_verified and report.crash_recovery_verified
    assert report.resume_verified and report.epoch2_verified
    assert report.cleanup.allowed
    assert all(epoch == 2 for epoch in report.cleanup.epoch_by_database.values())


def test_tampered_backup_is_refused_before_source_write(tmp_path):
    source = tmp_path / "memory.db"
    source.write_bytes(b"source-before-migration")
    backup = tmp_path / "backup"
    backup.mkdir()
    member = backup / source.name
    member.write_bytes(source.read_bytes())
    member.write_bytes(b"tampered")
    before = source.read_bytes()

    class Verifier:
        def verify(self, path):
            candidate = Path(path) / source.name
            return () if _sha256(candidate) == _sha256(source) else ("checksum mismatch",)

    try:
        verify_backup_before_migration(backup, {"memory": source}, Verifier())
    except BackupVerificationError:
        pass
    else:
        raise AssertionError("tampered backup was accepted")
    assert source.read_bytes() == before


def test_real_child_process_crash_leaves_restorable_pre_migration_bytes(tmp_path):
    home = tmp_path / "crashed"
    home.mkdir()
    _legacy_home(home)
    original = {name: _sha256(home / name) for name in DB_NAMES}
    repo = Path(__file__).resolve().parents[1]
    code = """
import os, sys
from pathlib import Path
from sonder_runtime.adapters.persistence.sqlite.bridge_migration import run_bridge_migration
home = Path(sys.argv[1])
def stop(step):
    if step == 'after_data_adoption':
        os._exit(77)
run_bridge_migration(home, version='data005-crash', step_hook=stop)
"""
    env = {**os.environ, "PYTHONPATH": str(repo)}
    crashed = subprocess.run(
        [sys.executable, "-c", code, str(home)],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert crashed.returncode == 77, crashed.stderr[-2000:]
    backups = sorted((home / "backups").glob("pre-epoch2-*"))
    assert len(backups) == 1
    backup = backups[0]
    restored = tmp_path / "restored"
    restored.mkdir()
    for name, digest in original.items():
        member = backup / name
        assert _sha256(member) == digest
        (restored / name).write_bytes(member.read_bytes())
    proof = BackupProof(str(backup), "data005", original)
    restore = prove_restore(proof, restored, {
        name: restored / name for name in original
    })
    assert restore.restored_digests == original
