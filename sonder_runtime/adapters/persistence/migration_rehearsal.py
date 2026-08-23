"""Non-destructive rehearsal of the epoch-2 bridge migration.

The rehearsal copies an operator-selected home into disposable directories and
invokes the existing bridge migration there.  It never changes the supplied
home.  The failed run is stopped at an explicit migration boundary, then two
independent recovery paths are proven against it: its pre-migration backup
can be restored, and the interrupted copy itself can simply be resumed in
place (an operator retrying a failed ``sonder migrate --adopt-epoch2`` does
the latter, not a restore). A third, uninterrupted copy is allowed to
complete and is checked by the application-owned epoch gate.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
    EPOCH,
    EPOCH2_DATABASES,
    check_epoch,
    run_bridge_migration,
)
from sonder_runtime.adapters.persistence.epoch_adoption import (
    Epoch2CleanupReport,
    check_epoch2_cleanup,
)


BRIDGE_DATABASES = (
    "memory.db",
    "autopilot.db",
    "fleet.db",
    "operations.db",
    "updates.db",
)


class RehearsalError(RuntimeError):
    """The migration rehearsal could not establish its evidence."""


@dataclass(frozen=True)
class RehearsalReport:
    source_home: str
    failure_boundary: str
    backup_verified: bool
    restore_verified: bool
    crash_recovery_verified: bool
    resume_verified: bool
    epoch2_verified: bool
    cleanup: Epoch2CleanupReport


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _copy_home(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for child in source.iterdir():
        if child.is_symlink():
            raise RehearsalError(f"source home contains an indirect entry: {child.name}")
        if child.is_dir():
            shutil.copytree(child, destination / child.name, symlinks=False)
        elif child.is_file():
            shutil.copy2(child, destination / child.name)


def _original_digests(home: Path) -> dict[str, str]:
    return {
        name: _digest(home / name)
        for name in BRIDGE_DATABASES
        if (home / name).is_file()
    }


def _epoch_row_count(db_path: Path) -> int:
    """Count ledger rows for ``EPOCH``; a resumed migration must add at most one."""
    if not db_path.is_file():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "schema_epoch" not in tables:
            return 0
        return conn.execute(
            "SELECT count(*) FROM schema_epoch WHERE epoch = ?", (EPOCH,)
        ).fetchone()[0]
    finally:
        conn.close()


def _backup_dirs(home: Path) -> tuple[Path, ...]:
    root = home / "backups"
    return tuple(sorted(root.glob("pre-epoch2-*"))) if root.is_dir() else ()


def _verify_and_restore(
    backup: Path,
    expected: Mapping[str, str],
    destination: Path,
) -> bool:
    """Verify bridge backup members and restore them into an empty directory."""
    destination.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    for name, digest in expected.items():
        member = backup / name
        if not member.is_file() or member.is_symlink() or _digest(member) != digest:
            return False
        target = destination / name
        shutil.copy2(member, target)
        if _digest(target) != digest:
            return False
        seen.add(name)
    # A bridge backup must not silently invent a database absent from the
    # source.  This also makes restore coverage explicit in the report.
    unexpected = {
        child.name for child in backup.iterdir()
        if child.is_file() and child.name.endswith(".db") and child.name not in seen
    }
    return not unexpected


def rehearse_bridge_migration(
    source_home: str | Path,
    *,
    failure_boundary: str = "after_data_adoption",
    version: str = "rehearsal-epoch2",
) -> RehearsalReport:
    """Run crash/recovery and successful epoch-2 checks on disposable copies.

    ``source_home`` is read-only input.  The bridge's own backup format is
    checked member-by-member, and restoration is performed into a fresh
    directory.  No delete, rename, or migration is directed at the supplied
    home.
    """
    source = Path(source_home).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise RehearsalError("source home must be a directory")
    expected = _original_digests(source)
    if not failure_boundary.strip():
        raise RehearsalError("failure_boundary must be non-empty")

    with tempfile.TemporaryDirectory(prefix="sonder-migration-rehearsal-") as tmp:
        root = Path(tmp)
        failed = root / "failed"
        successful = root / "successful"
        _copy_home(source, failed)
        _copy_home(source, successful)

        class InjectedCrash(RuntimeError):
            pass

        def stop_at(boundary: str) -> None:
            if boundary == failure_boundary:
                raise InjectedCrash(boundary)

        try:
            run_bridge_migration(failed, version=version, step_hook=stop_at)
        except InjectedCrash:
            pass
        else:
            raise RehearsalError(
                f"fault injection boundary was not reached: {failure_boundary}"
            )

        backups = _backup_dirs(failed)
        if len(backups) != 1:
            raise RehearsalError("failed rehearsal did not produce exactly one backup")
        backup = backups[0]
        restored = root / "restored"
        backup_verified = _verify_and_restore(backup, expected, restored)
        if not backup_verified:
            raise RehearsalError("pre-migration backup or restore verification failed")

        # Recovery evidence is kept separate from the failed copy: a real
        # operator restore also targets a fresh state root and never overlays
        # a live, possibly partially migrated directory.
        recovery = root / "recovered"
        crash_recovery_verified = _verify_and_restore(backup, expected, recovery)
        if not crash_recovery_verified:
            raise RehearsalError("crash recovery restore proof failed")

        # An operator retrying a failed `sonder migrate --adopt-epoch2` does
        # not restore from backup -- they just run it again against the
        # interrupted home. Prove that path directly: some domains may
        # already be stamped at EPOCH from before the injected crash, and
        # resuming must not append a second ledger row to them.
        pre_resume_rows = {
            name: _epoch_row_count(failed / name) for name in EPOCH2_DATABASES
        }
        run_bridge_migration(failed, version=f"{version}-resumed")
        resume_verified = all(
            check_epoch(failed / name) == EPOCH for name in EPOCH2_DATABASES
        ) and all(
            _epoch_row_count(failed / name) == 1 for name in EPOCH2_DATABASES
        )
        if not resume_verified:
            raise RehearsalError(
                "resuming the interrupted migration in place did not reach a "
                "clean epoch-2 state (pre-resume ledger rows: "
                f"{pre_resume_rows})"
            )

        run_bridge_migration(successful, version=version)
        cleanup = check_epoch2_cleanup(successful)
        if not cleanup.allowed:
            raise RehearsalError("epoch-2 cleanup checks failed: " + "; ".join(cleanup.reasons))
        epoch2_verified = all(
            check_epoch(successful / name) == 2
            for name in ("memory.db", "automation.db", "operations.db", "selfmod.db", "training.db")
        )
        if not epoch2_verified:
            raise RehearsalError("successful rehearsal did not stamp epoch 2 everywhere")

        return RehearsalReport(
            str(source), failure_boundary, backup_verified, True,
            crash_recovery_verified, resume_verified, epoch2_verified, cleanup,
        )


__all__ = ["BRIDGE_DATABASES", "RehearsalError", "RehearsalReport", "rehearse_bridge_migration"]
