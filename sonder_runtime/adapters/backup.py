"""Consistent, verified, atomically-published backup adapter (SPEC-2 section 13).

Layout of one backup:

    <target>/<UTC timestamp>-<version>-<backup_id>/
      manifest.json
      state/
        memory.db
        autopilot.db
        fleet.db
        operations.db
        runtime_policy.json
      checksums.sha256

Live SQLite databases are copied with the online backup API so a backup
taken during runtime activity is still consistent.  The staging directory
is verified file-by-file before one atomic rename publishes it; retention
runs only after the new backup is verified, and never removes the last
verified backup.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import runtime_policy
import sonder_migrations
import sonder_version
from sonder_operations_store import OperationsStore

MANIFEST_FORMAT_VERSION = 1


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    backup_id: str
    path: Path
    total_bytes: int
    file_count: int


def _utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_path(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _online_backup_sqlite(source: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source)
    try:
        dst = sqlite3.connect(str(destination))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _schema_versions() -> dict:
    versions: dict[str, dict] = {}
    for store, status in sonder_migrations.status_all().items():
        versions[store] = {
            "applied": list(status.applied),
            "pending": list(status.pending),
        }
    return versions


def create_backup(
    target: str | os.PathLike,
    *,
    operations: OperationsStore | None = None,
) -> BackupResult:
    """Run the full SPEC-2 backup algorithm against the live state."""
    target_dir = Path(target).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    build = sonder_version.build_info()
    ops = operations or OperationsStore()
    owner = f"backup-{os.getpid()}"
    ops.acquire_maintenance_lock(
        "backup", owner_id=owner, reason="backup in progress", ttl_seconds=3600
    )
    backup_id = ops.start_backup_run(build.version)
    staging = target_dir / f".staging-{backup_id}"
    try:
        state_dir = staging / "state"
        state_dir.mkdir(parents=True, exist_ok=False)

        copied: list[Path] = []
        for store, db_path in sonder_migrations.store_db_paths().items():
            if not Path(db_path).exists():
                continue
            destination = state_dir / f"{store}.db"
            _online_backup_sqlite(db_path, destination)
            copied.append(destination)

        policy_file = runtime_policy.policy_path()
        if policy_file.exists():
            destination = state_dir / "runtime_policy.json"
            shutil.copy2(policy_file, destination)
            copied.append(destination)

        if not copied:
            raise BackupError("no authoritative state found to back up")

        for path in copied:
            _fsync_path(path)
        _fsync_path(state_dir)

        files = []
        total_bytes = 0
        for path in sorted(copied):
            size = path.stat().st_size
            total_bytes += size
            files.append(
                {
                    "path": str(path.relative_to(staging)),
                    "size": size,
                    "sha256": _sha256_file(path),
                    "classification": "private",
                }
            )
        now = time.time()
        manifest = {
            "format_version": MANIFEST_FORMAT_VERSION,
            "backup_id": backup_id,
            # Microsecond precision so backups taken within the same second
            # still order deterministically in list/prune.
            "created_at_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(now)
            )
            + f".{int((now % 1) * 1_000_000):06d}Z",
            "application_version": build.version,
            "commit_sha": build.commit_sha,
            "schema_versions": _schema_versions(),
            "files": files,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        checksums = staging / "checksums.sha256"
        checksums.write_text(
            "".join(f"{f['sha256']}  {f['path']}\n" for f in files)
            + f"{_sha256_file(manifest_path)}  manifest.json\n",
            encoding="utf-8",
        )
        _fsync_path(manifest_path)
        _fsync_path(checksums)
        _fsync_path(staging)

        problems = _verify_directory(staging)
        if problems:
            raise BackupError("staged backup failed verification: " + "; ".join(problems))

        final_name = f"{_utc_stamp()}-{build.version}-{backup_id}"
        final_path = target_dir / final_name
        os.rename(staging, final_path)
        _fsync_path(target_dir)

        ops.finish_backup_run(
            backup_id,
            status="verified",
            manifest_path=str(final_path / "manifest.json"),
            total_bytes=total_bytes,
        )
        ops.record_event(
            component="backup",
            event_code="BACKUP_COMPLETED",
            summary=f"backup {backup_id} verified",
            detail={
                "backup_id": backup_id,
                "files": len(files),
                "total_bytes": total_bytes,
            },
            operation_id=backup_id,
        )
        return BackupResult(
            backup_id=backup_id,
            path=final_path,
            total_bytes=total_bytes,
            file_count=len(files),
        )
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            ops.finish_backup_run(
                backup_id, status="failed", error_code=type(exc).__name__
            )
            ops.record_event(
                component="backup",
                event_code="BACKUP_FAILED",
                severity="ERROR",
                summary=f"backup {backup_id} failed",
                detail={"backup_id": backup_id, "error": type(exc).__name__},
                operation_id=backup_id,
            )
        except Exception:
            pass
        raise
    finally:
        ops.release_maintenance_lock("backup", owner_id=owner)


def _verify_directory(backup_dir: Path) -> list[str]:
    problems: list[str] = []
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        return [f"missing manifest.json in {backup_dir}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"manifest.json unreadable: {exc}"]
    if manifest.get("format_version") != MANIFEST_FORMAT_VERSION:
        problems.append(
            f"unsupported manifest format_version {manifest.get('format_version')!r}"
        )
        return problems
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        problems.append("manifest lists no files")
        return problems
    for entry in files:
        rel = entry.get("path", "")
        member = backup_dir / rel
        if ".." in Path(rel).parts or Path(rel).is_absolute():
            problems.append(f"manifest path escapes backup: {rel!r}")
            continue
        if not member.exists():
            problems.append(f"missing file {rel}")
            continue
        size = member.stat().st_size
        if size != entry.get("size"):
            problems.append(
                f"size mismatch for {rel}: {size} != {entry.get('size')}"
            )
            continue
        if _sha256_file(member) != entry.get("sha256"):
            problems.append(f"checksum mismatch for {rel}")
    return problems


def verify_backup(backup_dir: str | os.PathLike) -> list[str]:
    """Return a list of problems; an empty list means the backup verifies."""
    return _verify_directory(Path(backup_dir).expanduser())


def list_backups(target: str | os.PathLike) -> list[dict]:
    target_dir = Path(target).expanduser()
    if not target_dir.is_dir():
        return []
    entries = []
    for child in sorted(target_dir.iterdir()):
        if child.name.startswith(".staging-") or not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        entries.append(
            {
                "path": str(child),
                "backup_id": manifest.get("backup_id", "unknown"),
                "created_at_utc": manifest.get("created_at_utc", "unknown"),
                "application_version": manifest.get(
                    "application_version", "unknown"
                ),
                "files": len(manifest.get("files") or []),
            }
        )
    entries.sort(key=lambda e: e["created_at_utc"], reverse=True)
    return entries


def prune_backups(target: str | os.PathLike, *, keep: int) -> list[str]:
    """Remove oldest backups beyond ``keep``; never removes the newest
    verified backup.  Tiered daily/weekly/monthly retention arrives with
    the WP7 scheduler; this primitive is deliberately conservative."""
    if keep < 1:
        raise BackupError("retention must keep at least one backup")
    backups = list_backups(target)
    verified_newest: str | None = None
    for entry in backups:
        if not verify_backup(entry["path"]):
            verified_newest = entry["path"]
            break
    removed = []
    for entry in backups[keep:]:
        if entry["path"] == verified_newest:
            continue
        shutil.rmtree(entry["path"])
        removed.append(entry["path"])
    return removed


def prune_backups_tiered(
    target: str | os.PathLike,
    *,
    daily: int = 7,
    weekly: int = 4,
    monthly: int = 6,
) -> list[str]:
    """Grandfather-father-son retention (SPEC-2 section 3 backup config).

    Keeps the newest backup of each of the last ``daily`` days, the newest
    of each of the last ``weekly`` ISO weeks, and the newest of each of the
    last ``monthly`` months.  The newest verified backup is always kept.
    """
    if min(daily, weekly, monthly) < 1:
        raise BackupError("every retention tier must keep at least one backup")
    backups = list_backups(target)
    if not backups:
        return []

    keep: set[str] = set()
    newest_verified: str | None = None
    for entry in backups:
        if not verify_backup(entry["path"]):
            newest_verified = entry["path"]
            break
    if newest_verified:
        keep.add(newest_verified)

    day_buckets: dict[str, str] = {}
    week_buckets: dict[str, str] = {}
    month_buckets: dict[str, str] = {}
    for entry in backups:  # newest first
        stamp = entry["created_at_utc"]
        day = stamp[:10]
        month = stamp[:7]
        try:
            import datetime

            iso = datetime.date.fromisoformat(day).isocalendar()
            week = f"{iso.year}-W{iso.week:02d}"
        except ValueError:
            week = day
        day_buckets.setdefault(day, entry["path"])
        week_buckets.setdefault(week, entry["path"])
        month_buckets.setdefault(month, entry["path"])
    keep.update(list(day_buckets.values())[:daily])
    keep.update(list(week_buckets.values())[:weekly])
    keep.update(list(month_buckets.values())[:monthly])

    removed = []
    for entry in backups:
        if entry["path"] in keep:
            continue
        shutil.rmtree(entry["path"])
        removed.append(entry["path"])
    return removed


def restore_smoke(backup_dir: str | os.PathLike) -> list[str]:
    """Restore into a disposable directory and prove the state is usable.

    Checks performed on the restored copy: manifest verification, SQLite
    ``PRAGMA integrity_check`` and ``PRAGMA foreign_key_check`` on every
    database, and migration-ledger health (no unknown or tampered
    migrations).  Returns a list of problems; empty means the smoke passed.
    """
    import tempfile

    problems = verify_backup(backup_dir)
    if problems:
        return [f"verification: {p}" for p in problems]
    with tempfile.TemporaryDirectory(prefix="sonder-restore-smoke-") as tmp:
        dest = Path(tmp) / "state"
        try:
            restore_to_empty(backup_dir, dest)
        except BackupError as exc:
            return [f"restore: {exc}"]
        for db_file in sorted(dest.glob("*.db")):
            conn = sqlite3.connect(str(db_file))
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if row is None or row[0] != "ok":
                    problems.append(
                        f"{db_file.name}: integrity_check {row[0] if row else 'empty'}"
                    )
                fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
                if fk_rows:
                    problems.append(
                        f"{db_file.name}: {len(fk_rows)} foreign key violations"
                    )
            finally:
                conn.close()
            store = db_file.stem
            if store in sonder_migrations.store_db_paths():
                status = sonder_migrations.status(store, str(db_file))
                if status.unknown:
                    problems.append(
                        f"{db_file.name}: schema newer than this build "
                        f"({list(status.unknown)})"
                    )
                if status.checksum_mismatches:
                    problems.append(
                        f"{db_file.name}: migration history modified"
                    )
    return problems


def restore_to_empty(
    backup_dir: str | os.PathLike, destination: str | os.PathLike
) -> list[str]:
    """Restore a verified backup's state files into an *empty* directory.

    Pointer switching / service stop-start is an operator runbook step;
    this function refuses a non-empty destination so it can never clobber
    a live SONDER_HOME.
    """
    source = Path(backup_dir).expanduser()
    problems = verify_backup(source)
    if problems:
        raise BackupError(
            "backup failed verification: " + "; ".join(problems)
        )
    dest = Path(destination).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise BackupError(f"restore destination {dest} is not empty")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    restored = []
    for entry in manifest["files"]:
        rel = Path(entry["path"])
        # verified above: relative, no traversal
        target_path = dest / rel.name
        shutil.copy2(source / rel, target_path)
        if _sha256_file(target_path) != entry["sha256"]:
            raise BackupError(f"restored file corrupt: {rel.name}")
        restored.append(str(target_path))
    return restored
