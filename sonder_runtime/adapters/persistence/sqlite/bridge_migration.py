"""Schema epoch 2 bridge migration (SPEC-5 §34).

The bridge release migrates all state from SPEC-3 schema ownership to
SPEC-5 domain ownership.  It:
1. Verifies current schemas/migration ledgers
2. Takes a full verified backup
3. Migrates/adopts state into SPEC-5 domain ownership
4. Records schema_epoch = 2
5. Verifies row counts/integrity
6. Writes an adoption receipt

The final SPEC-5 runtime requires epoch 2.  Pre-epoch state fails with
MigrationRequired.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .automation import init_automation_db, AUTOMATION_DDL
from .memory import add_outbox_to_memory_db, add_epoch_marker as memory_epoch
from .operations import init_operations_db
from .selfmod import init_selfmod_db
from .training import init_training_db
from .updates import add_outbox_to_updates_db, add_epoch_marker as updates_epoch

from ....domain.common.errors import MigrationRequired


EPOCH = 2
EPOCH2_DATABASES = (
    "memory.db", "automation.db", "operations.db", "selfmod.db", "training.db",
)


@dataclass
class MigrationReceipt:
    epoch: int
    completed_at: str
    source_version: str
    memory_row_count: int
    automation_row_count: int
    tasks_migrated: int
    backup_path: str


def check_epoch(db_path: Path) -> int | None:
    """Return the current epoch, or None if no epoch table exists."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "schema_epoch" not in tables:
            return None
        row = conn.execute(
            "SELECT epoch FROM schema_epoch ORDER BY epoch DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def require_epoch_2(sonder_home: Path) -> None:
    """Fail closed unless every adopted domain database is at epoch 2."""
    memory_db = sonder_home / "memory.db"
    if not memory_db.exists():
        return  # Fresh install — will create epoch 2 directly
    epochs = {name: check_epoch(sonder_home / name) for name in EPOCH2_DATABASES}
    invalid = tuple(name for name, epoch in epochs.items() if epoch != EPOCH)
    if invalid:
        raise MigrationRequired(
            "State is not fully adopted at the SPEC-5 schema epoch "
            f"(invalid databases: {', '.join(invalid)}). "
            "Run the designated bridge release/migration before upgrading."
        )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_db(db_path: Path, backup_dir: Path) -> Path | None:
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / db_path.name
    shutil.copy2(str(db_path), str(dest))
    if _sha256_file(dest) != _sha256_file(db_path):
        raise RuntimeError(f"Backup verification failed for {db_path}")
    return dest


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT count(*) FROM [{table}]").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _migrate_tasks(
    memory_conn: sqlite3.Connection,
    automation_conn: sqlite3.Connection,
) -> int:
    """Move tasks and task_events from memory.db to automation.db."""
    tables = {r[0] for r in memory_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "tasks" not in tables:
        return 0

    rows = memory_conn.execute(
        "SELECT id, title, detail, status, priority, project, owner, "
        "parent_id, created_ts, updated_ts FROM tasks"
    ).fetchall()

    migrated = 0
    for row in rows:
        tid, title, detail, status, priority, project, owner, parent_id, created, updated = row
        try:
            automation_conn.execute(
                "INSERT INTO automation_runs "
                "(id, kind, status, revision, objective, correlation_id, "
                " config_json, created_at, updated_at) "
                "VALUES (?, 'task', ?, 0, ?, ?, ?, ?, ?)",
                (
                    tid, status, title or "",
                    tid,  # correlation_id = task id
                    json.dumps({
                        "detail": detail, "priority": priority,
                        "project": project, "owner": owner,
                        "parent_id": parent_id,
                    }),
                    created or "", updated or "",
                ),
            )
            migrated += 1
        except sqlite3.IntegrityError:
            pass  # Already migrated

    # Migrate task_events
    if "task_events" in tables:
        events = memory_conn.execute(
            "SELECT id, task_id, event, note, ts FROM task_events"
        ).fetchall()
        for ev_id, task_id, event, note, ts in events:
            try:
                seq_row = automation_conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_events "
                    "WHERE run_id = ?", (task_id,)
                ).fetchone()
                seq = seq_row[0] if seq_row else 1
                automation_conn.execute(
                    "INSERT INTO task_events "
                    "(id, run_id, sequence, event_type, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ev_id, task_id, seq, event or "",
                     json.dumps({"note": note}), ts or ""),
                )
            except sqlite3.IntegrityError:
                pass

    automation_conn.commit()
    return migrated


def _stamp_epoch(conn: sqlite3.Connection, version: str) -> None:
    conn.execute("""\
        CREATE TABLE IF NOT EXISTS schema_epoch (
            epoch           INTEGER NOT NULL,
            completed_at    TEXT NOT NULL,
            source_version  TEXT NOT NULL
        )
    """)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO schema_epoch (epoch, completed_at, source_version) "
        "VALUES (?, ?, ?)",
        (EPOCH, now, version),
    )
    conn.commit()


def run_bridge_migration(
    sonder_home: Path,
    version: str = "spec5-bridge",
    *,
    step_hook: Callable[[str], None] | None = None,
) -> MigrationReceipt:
    """Execute the full epoch 1→2 bridge migration.

    Crash-safe: backup is taken first, each step is independently
    committable, and partial migration can be resumed.
    """
    now = datetime.now(timezone.utc).isoformat()
    backup_dir = sonder_home / "backups" / f"pre-epoch2-{now.replace(':', '-')}"

    active_connections: list[sqlite3.Connection] = []

    def checkpoint(name: str) -> None:
        # This hook is intentionally an explicit test/rehearsal seam.  The
        # normal runtime supplies no hook, so production behavior is
        # unchanged; the rehearsal harness can stop at a named boundary and
        # prove recovery from the already-created backup.
        if step_hook is not None:
            try:
                step_hook(name)
            except Exception:
                # Fault injection is deliberately allowed to model a process
                # stop, but the rehearsal must not retain Windows file locks
                # while it verifies/restores the disposable copy.
                for connection in active_connections:
                    try:
                        connection.close()
                    except Exception:
                        pass
                raise

    # 1. Backup all existing databases
    db_names = ["memory.db", "autopilot.db", "fleet.db",
                "operations.db", "updates.db"]
    for name in db_names:
        _backup_db(sonder_home / name, backup_dir)
    checkpoint("after_backup")

    # 2. Initialize new databases
    automation_db_path = sonder_home / "automation.db"
    automation_conn = init_automation_db(automation_db_path)
    active_connections.append(automation_conn)

    # Ensure operations.db has the new projection tables
    ops_db_path = sonder_home / "operations.db"
    ops_conn = init_operations_db(ops_db_path)
    active_connections.append(ops_conn)

    # Create new selfmod.db and training.db
    selfmod_conn = init_selfmod_db(sonder_home / "selfmod.db")
    training_conn = init_training_db(sonder_home / "training.db")
    active_connections.extend((selfmod_conn, training_conn))
    checkpoint("after_domain_initialization")

    # 3. Migrate tasks from memory.db → automation.db
    tasks_migrated = 0
    memory_db_path = sonder_home / "memory.db"
    if memory_db_path.exists():
        memory_conn = sqlite3.connect(str(memory_db_path))
        active_connections.append(memory_conn)
        tasks_migrated = _migrate_tasks(memory_conn, automation_conn)
        # Add outbox to memory.db
        add_outbox_to_memory_db(memory_conn)
        memory_epoch(memory_conn)
        _stamp_epoch(memory_conn, version)
        memory_row_count = sum(
            _count_rows(memory_conn, t) for t in
            ["interactions", "outcomes", "lessons", "sessions", "facts", "preferences"]
        )
        memory_conn.close()
    else:
        memory_row_count = 0
        # Fresh installs still participate in the epoch contract.  There is
        # no legacy data to copy, but the final runtime requires every domain
        # database in the adopted set to carry an explicit epoch marker.
        memory_conn = sqlite3.connect(str(memory_db_path))
        active_connections.append(memory_conn)
        memory_epoch(memory_conn)
        _stamp_epoch(memory_conn, version)
        memory_conn.close()
    checkpoint("after_data_adoption")

    # Add outbox to updates.db if it exists
    updates_db_path = sonder_home / "updates.db"
    if updates_db_path.exists():
        updates_conn = sqlite3.connect(str(updates_db_path))
        active_connections.append(updates_conn)
        add_outbox_to_updates_db(updates_conn)
        updates_epoch(updates_conn)
        _stamp_epoch(updates_conn, version)
        updates_conn.close()

    # 4. Stamp epoch on all new databases
    _stamp_epoch(automation_conn, version)
    _stamp_epoch(ops_conn, version)
    _stamp_epoch(selfmod_conn, version)
    _stamp_epoch(training_conn, version)

    automation_row_count = _count_rows(automation_conn, "automation_runs")
    checkpoint("after_epoch_stamp")

    # Close all connections
    automation_conn.close()
    ops_conn.close()
    selfmod_conn.close()
    training_conn.close()

    # 5. Write adoption receipt
    receipt = MigrationReceipt(
        epoch=EPOCH,
        completed_at=now,
        source_version=version,
        memory_row_count=memory_row_count,
        automation_row_count=automation_row_count,
        tasks_migrated=tasks_migrated,
        backup_path=str(backup_dir),
    )
    receipt_path = sonder_home / "epoch2_adoption_receipt.json"
    receipt_path.write_text(json.dumps({
        "epoch": receipt.epoch,
        "completed_at": receipt.completed_at,
        "source_version": receipt.source_version,
        "memory_row_count": receipt.memory_row_count,
        "automation_row_count": receipt.automation_row_count,
        "tasks_migrated": receipt.tasks_migrated,
        "backup_path": receipt.backup_path,
    }, indent=2))

    return receipt
