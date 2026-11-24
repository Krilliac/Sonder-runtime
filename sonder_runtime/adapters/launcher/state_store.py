"""Persistent SQLite operation ledger and health-token provisioning for the launcher."""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sonder_runtime.domain import launcher_health as sonder_health


ACTIVE_PHASES = ("queued", "running")
TERMINAL_PHASES = ("succeeded", "failed", "cancelled", "interrupted")
OPERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sonder_launcher_operations (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    context_size TEXT NOT NULL,
    phase TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    created_ts REAL NOT NULL,
    started_ts REAL,
    updated_ts REAL NOT NULL,
    finished_ts REAL,
    owner_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    owner_host TEXT NOT NULL,
    lease_until REAL NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    commands_json TEXT NOT NULL DEFAULT '[]',
    hard_deadline REAL,
    control_pid INTEGER,
    control_identity TEXT,
    control_group_id INTEGER,
    control_platform TEXT,
    control_started_ts REAL,
    control_finished_ts REAL,
    control_exit_code INTEGER
);
CREATE INDEX IF NOT EXISTS sonder_launcher_operations_phase_created
    ON sonder_launcher_operations(phase, created_ts DESC);
CREATE TABLE IF NOT EXISTS sonder_launcher_operation_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    operation_id TEXT,
    owner_id TEXT,
    owner_pid INTEGER,
    owner_host TEXT,
    lease_until REAL
);
INSERT OR IGNORE INTO sonder_launcher_operation_lock(id) VALUES (1);
"""

OPERATION_MIGRATIONS: dict[str, str] = {
    "hard_deadline": "REAL",
    "control_pid": "INTEGER",
    "control_identity": "TEXT",
    "control_group_id": "INTEGER",
    "control_platform": "TEXT",
    "control_started_ts": "REAL",
    "control_finished_ts": "REAL",
    "control_exit_code": "INTEGER",
}


class LauncherConflictError(RuntimeError):
    """A second operation or mismatched idempotent replay was rejected."""

    def __init__(self, message: str, operation: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.operation = operation


class ControlTreeNotStopped(RuntimeError):
    """The launcher must retain its lock because owned control work survived."""


def write_private_file(path: str | Path, value: str) -> None:
    """Atomically write ``value`` to a 0600 private file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    temporary = target.with_name(".%s.%s.tmp" % (target.name, uuid.uuid4().hex))
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def provision_health_token(path: str | Path, configured: str = "") -> str:
    """Validate or provision the persistent launcher health token."""
    configured_value = str(configured or "").strip()
    if configured_value and len(configured_value) < sonder_health.MIN_TOKEN_LENGTH:
        raise ValueError(
            "%s must contain at least %s characters"
            % (sonder_health.TOKEN_ENV, sonder_health.MIN_TOKEN_LENGTH)
        )
    token_path = Path(path)
    if token_path.is_symlink():
        raise ValueError("launcher health token path must not be a symbolic link")
    exists = token_path.exists()
    try:
        existing = token_path.read_text(encoding="ascii").strip() if exists else ""
    except (OSError, UnicodeError) as exc:
        raise ValueError("persisted launcher health token could not be read") from exc
    if exists and len(existing) < sonder_health.MIN_TOKEN_LENGTH:
        raise ValueError("persisted launcher health token is invalid")
    if configured_value:
        if existing and existing != configured_value:
            raise ValueError(
                "configured launcher health token does not match the persisted token"
            )
        if not existing:
            write_private_file(token_path, configured_value)
        return configured_value
    if len(existing) >= sonder_health.MIN_TOKEN_LENGTH:
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
        return existing
    token = secrets.token_urlsafe(32)
    write_private_file(token_path, token)
    return token


def connect_database(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection configured with row factory and busy timeout."""
    connection = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def initialize_database(
    db_path: Path,
    connect_fn: Callable[[], sqlite3.Connection],
) -> None:
    """Create and migrate the launcher operation tables."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_fn()
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(sonder_launcher_operations)"
            ).fetchall()
        }
        for name, sql_type in OPERATION_MIGRATIONS.items():
            if name not in columns:
                connection.execute(
                    "ALTER TABLE sonder_launcher_operations ADD COLUMN %s %s"
                    % (name, sql_type)
                )
        connection.commit()
    finally:
        connection.close()
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass


def provision_controller_health_token(
    connect_fn: Callable[[], sqlite3.Connection],
    health_token_path: Path,
    configured: str,
    *,
    provision_fn: Callable[[str | Path, str], str] = provision_health_token,
) -> str:
    """Serialize first-run health-token creation behind the SQLite write lock."""
    connection = connect_fn()
    try:
        connection.execute("BEGIN IMMEDIATE")
        token = provision_fn(health_token_path, configured)
        connection.commit()
        return token
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def operation_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Decode a ``sonder_launcher_operations`` row into its public dict shape."""
    if row is None:
        return None
    try:
        commands = json.loads(row["commands_json"] or "[]")
    except (TypeError, ValueError):
        commands = []
    if not isinstance(commands, list):
        commands = []
    return {
        "id": row["id"],
        "action": row["action"],
        "context_size": row["context_size"],
        "phase": row["phase"],
        "created_ts": row["created_ts"],
        "started_ts": row["started_ts"],
        "updated_ts": row["updated_ts"],
        "finished_ts": row["finished_ts"],
        "message": row["message"] or "",
        "last_error": row["last_error"] or "",
        "command": commands[-1] if commands else [],
        "commands": commands,
    }


def mark_recovery_blocked(
    connect_fn: Callable[[], sqlite3.Connection],
    operation_id: str,
    owner_id: str,
) -> None:
    """Record that recovery could not prove a stale control tree stopped."""
    connection = connect_fn()
    try:
        with connection:
            connection.execute(
                """
                UPDATE sonder_launcher_operations
                SET updated_ts=?, last_error=?, message=?
                WHERE id=? AND owner_id=? AND phase IN ('queued','running')
                """,
                (
                    time.time(),
                    "stale control process tree could not be proven stopped",
                    "Recovery is blocked; the operation lock remains held.",
                    operation_id,
                    owner_id,
                ),
            )
    finally:
        connection.close()


def interrupt_recovered_operation(
    connect_fn: Callable[[], sqlite3.Connection],
    lock: Mapping[str, Any],
    operation: Mapping[str, Any],
    reason: str,
) -> bool:
    """Atomically transition a recovered active operation to ``interrupted``."""
    now = time.time()
    connection = connect_fn()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_lock = connection.execute(
            "SELECT * FROM sonder_launcher_operation_lock WHERE id=1"
        ).fetchone()
        current = connection.execute(
            "SELECT * FROM sonder_launcher_operations WHERE id=?",
            (operation["id"],),
        ).fetchone()
        if (
            not current_lock
            or current_lock["operation_id"] != operation["id"]
            or current_lock["owner_id"] != lock.get("owner_id")
            or not current
            or current["owner_id"] != operation.get("owner_id")
            or current["phase"] not in ACTIVE_PHASES
            or current["control_pid"] != operation.get("control_pid")
            or (current["control_identity"] or "")
            != (operation.get("control_identity") or "")
        ):
            connection.rollback()
            return False
        connection.execute(
            """
            UPDATE sonder_launcher_operations
            SET phase='interrupted', updated_ts=?, finished_ts=?,
                last_error=?, message=? WHERE id=?
            """,
            (
                now,
                now,
                reason,
                "Operation interrupted; inspect server status before retrying.",
                operation["id"],
            ),
        )
        connection.execute(
            """
            UPDATE sonder_launcher_operation_lock
            SET operation_id=NULL, owner_id=NULL, owner_pid=NULL,
                owner_host=NULL, lease_until=NULL
            WHERE id=1 AND operation_id=? AND owner_id=?
            """,
            (operation["id"], lock.get("owner_id")),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_lock_and_operation(
    connect_fn: Callable[[], sqlite3.Connection],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Read the singleton lock row and its referenced operation row, if any."""
    connection = connect_fn()
    try:
        lock_row = connection.execute(
            "SELECT * FROM sonder_launcher_operation_lock WHERE id=1"
        ).fetchone()
        lock = dict(lock_row) if lock_row else {}
        operation_row = None
        if lock.get("operation_id"):
            operation_row = connection.execute(
                "SELECT * FROM sonder_launcher_operations WHERE id=?",
                (lock["operation_id"],),
            ).fetchone()
        operation = dict(operation_row) if operation_row else None
        return lock, operation
    finally:
        connection.close()


def clear_operation_lock(
    connect_fn: Callable[[], sqlite3.Connection],
    operation_id: str,
    owner_id: Any,
) -> None:
    """Release the singleton lock if it still points to ``(operation_id, owner_id)``."""
    connection = connect_fn()
    try:
        with connection:
            connection.execute(
                """
                UPDATE sonder_launcher_operation_lock
                SET operation_id=NULL, owner_id=NULL, owner_pid=NULL,
                    owner_host=NULL, lease_until=NULL
                WHERE id=1 AND operation_id=? AND owner_id=?
                """,
                (operation_id, owner_id),
            )
    finally:
        connection.close()


def persist_control_started(
    connect_fn: Callable[[], sqlite3.Connection],
    operation_id: str,
    owner_id: str,
    pid: int,
    identity: str,
    group_id: int,
    platform_name: str,
    *,
    retry_delays: Sequence[float],
) -> None:
    """Persist launched control-process metadata while holding ownership."""
    last_error: sqlite3.Error | None = None
    for delay in retry_delays:
        if delay:
            time.sleep(delay)
        connection: sqlite3.Connection | None = None
        try:
            connection = connect_fn()
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE sonder_launcher_operations
                SET control_pid=?, control_identity=?, control_group_id=?,
                    control_platform=?, control_started_ts=?,
                    control_finished_ts=NULL, control_exit_code=NULL,
                    updated_ts=?
                WHERE id=? AND owner_id=? AND phase='running'
                  AND EXISTS (
                      SELECT 1 FROM sonder_launcher_operation_lock
                      WHERE id=1 AND operation_id=? AND owner_id=?
                  )
                """,
                (
                    int(pid),
                    identity,
                    int(group_id),
                    platform_name,
                    time.time(),
                    time.time(),
                    operation_id,
                    owner_id,
                    operation_id,
                    owner_id,
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise LauncherConflictError(
                    "launcher operation lost ownership before command start"
                )
            connection.commit()
            return
        except LauncherConflictError:
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            last_error = exc
        finally:
            if connection is not None:
                connection.close()
    raise last_error or sqlite3.OperationalError(
        "control process metadata could not be persisted"
    )


def persist_control_finished(
    connect_fn: Callable[[], sqlite3.Connection],
    operation_id: str,
    owner_id: str,
    pid: int,
    identity: str,
    return_code: int,
    *,
    retry_delays: Sequence[float],
) -> None:
    """Persist control-process exit metadata while holding ownership."""
    last_error: sqlite3.Error | None = None
    for delay in retry_delays:
        if delay:
            time.sleep(delay)
        connection: sqlite3.Connection | None = None
        try:
            connection = connect_fn()
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE sonder_launcher_operations
                SET control_finished_ts=?, control_exit_code=?, updated_ts=?
                WHERE id=? AND owner_id=? AND phase='running'
                  AND control_pid=? AND control_identity=?
                  AND EXISTS (
                      SELECT 1 FROM sonder_launcher_operation_lock
                      WHERE id=1 AND operation_id=? AND owner_id=?
                  )
                """,
                (
                    time.time(),
                    int(return_code),
                    time.time(),
                    operation_id,
                    owner_id,
                    int(pid),
                    identity,
                    operation_id,
                    owner_id,
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise LauncherConflictError(
                    "launcher operation lost ownership before command completion"
                )
            connection.commit()
            return
        except LauncherConflictError:
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            last_error = exc
        finally:
            if connection is not None:
                connection.close()
    raise last_error or sqlite3.OperationalError(
        "control process completion could not be persisted"
    )


def latest_operations(
    connect_fn: Callable[[], sqlite3.Connection],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(active_operation, latest_operation)`` from the SQLite ledger."""
    connection = connect_fn()
    try:
        active = connection.execute(
            """
            SELECT * FROM sonder_launcher_operations
            WHERE phase IN ('queued','running')
            ORDER BY created_ts DESC LIMIT 1
            """
        ).fetchone()
        latest = connection.execute(
            "SELECT * FROM sonder_launcher_operations ORDER BY created_ts DESC LIMIT 1"
        ).fetchone()
        return operation_from_row(active), operation_from_row(latest)
    finally:
        connection.close()


def load_operation(
    connect_fn: Callable[[], sqlite3.Connection],
    operation_id: str,
) -> dict[str, Any] | None:
    """Fetch a single operation record by normalized hex ID."""
    connection = connect_fn()
    try:
        row = connection.execute(
            "SELECT * FROM sonder_launcher_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        return operation_from_row(row)
    finally:
        connection.close()


def submit_operation_record(
    connect_fn: Callable[[], sqlite3.Connection],
    *,
    operation_id: str,
    owner_id: str,
    action: str,
    context_size: str,
    idempotency_key: str,
    now: float,
    lease_until: float,
    hard_deadline: float,
    register_worker_fn: Callable[[str, str], None],
    unregister_worker_fn: Callable[[str, str], None],
) -> tuple[dict[str, Any], bool]:
    """Insert a queued operation and acquire the singleton lock, or replay idempotently."""
    connection = connect_fn()
    worker_registered = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            existing = connection.execute(
                "SELECT * FROM sonder_launcher_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                operation = operation_from_row(existing)
                assert operation is not None
                if (
                    operation["action"] != action
                    or operation["context_size"] != context_size
                ):
                    raise LauncherConflictError(
                        "Idempotency-Key was already used for a different request",
                        operation,
                    )
                connection.commit()
                return operation, False

        lock = connection.execute(
            "SELECT * FROM sonder_launcher_operation_lock WHERE id=1"
        ).fetchone()
        if lock and lock["operation_id"]:
            active = connection.execute(
                "SELECT * FROM sonder_launcher_operations WHERE id=?",
                (lock["operation_id"],),
            ).fetchone()
            raise LauncherConflictError(
                "another launcher operation is already active",
                operation_from_row(active),
            )

        connection.execute(
            """
            INSERT INTO sonder_launcher_operations(
                id,action,context_size,phase,idempotency_key,created_ts,
                updated_ts,owner_id,owner_pid,owner_host,lease_until,
                hard_deadline
            ) VALUES(?,?,?,'queued',?,?,?,?,?,?,?,?)
            """,
            (
                operation_id,
                action,
                context_size,
                idempotency_key or None,
                now,
                now,
                owner_id,
                os.getpid(),
                socket.gethostname(),
                lease_until,
                hard_deadline,
            ),
        )
        connection.execute(
            """
            UPDATE sonder_launcher_operation_lock
            SET operation_id=?, owner_id=?, owner_pid=?, owner_host=?,
                lease_until=? WHERE id=1
            """,
            (
                operation_id,
                owner_id,
                os.getpid(),
                socket.gethostname(),
                lease_until,
            ),
        )
        row = connection.execute(
            "SELECT * FROM sonder_launcher_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        register_worker_fn(operation_id, owner_id)
        worker_registered = True
        connection.commit()
    except Exception:
        connection.rollback()
        if worker_registered:
            unregister_worker_fn(operation_id, owner_id)
        raise
    finally:
        connection.close()

    operation = operation_from_row(row)
    assert operation is not None
    return operation, True


def mark_operation_running(
    connect_fn: Callable[[], sqlite3.Connection],
    operation_id: str,
    owner_id: str,
) -> bool:
    """Transition a queued operation to ``running`` while confirming lock ownership."""
    now = time.time()
    connection = connect_fn()
    try:
        with connection:
            updated = connection.execute(
                """
                UPDATE sonder_launcher_operations
                SET phase='running', started_ts=?, updated_ts=?
                WHERE id=? AND owner_id=? AND phase='queued'
                  AND EXISTS (
                      SELECT 1 FROM sonder_launcher_operation_lock
                      WHERE id=1 AND operation_id=? AND owner_id=?
                  )
                """,
                (
                    now,
                    now,
                    operation_id,
                    owner_id,
                    operation_id,
                    owner_id,
                ),
            ).rowcount
    finally:
        connection.close()
    return updated == 1


def finish_operation_with_retry(
    *,
    operation_id: str,
    owner_id: str,
    phase: str,
    message: str,
    last_error: str,
    commands: object,
    retry_delays: Sequence[float],
    finish_fn: Callable[[str, str, str, str, str, object], bool],
    emergency_finish_fn: Callable[[str, str, str, str, str, object], bool],
) -> bool:
    """Finalize an operation with bounded retry and independent emergency fallback."""
    error: BaseException | None = None
    for delay in retry_delays:
        if delay:
            time.sleep(delay)
        try:
            return finish_fn(
                operation_id,
                owner_id,
                phase,
                message,
                last_error,
                commands,
            )
        except (OSError, sqlite3.Error) as exc:
            error = exc
    try:
        return emergency_finish_fn(
            operation_id,
            owner_id,
            phase,
            message,
            last_error or ("finalization retry failed: %s" % error),
            commands,
        )
    except (OSError, sqlite3.Error):
        return False


def emergency_finish_operation(
    db_path: Path,
    operation_id: str,
    owner_id: str,
    phase: str,
    message: str,
    last_error: str,
    commands: object,
    *,
    max_operation_output: int,
    output_text_fn: Callable[..., str],
) -> bool:
    """Minimal independent terminal transaction after normal retries fail."""
    now = time.time()
    commands_json = json.dumps(
        commands if isinstance(commands, list) else [], separators=(",", ":")
    )
    if len(commands_json) > max_operation_output:
        commands_json = "[]"
    connection = sqlite3.connect(str(db_path), timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE sonder_launcher_operations
            SET phase=?, updated_ts=?, finished_ts=?, message=?,
                last_error=?, commands_json=?
            WHERE id=? AND owner_id=? AND phase IN ('queued','running')
              AND EXISTS (
                  SELECT 1 FROM sonder_launcher_operation_lock
                  WHERE id=1 AND operation_id=? AND owner_id=?
              )
            """,
            (
                phase,
                now,
                now,
                output_text_fn(message),
                output_text_fn(last_error),
                commands_json,
                operation_id,
                owner_id,
                operation_id,
                owner_id,
            ),
        ).rowcount
        if updated == 1:
            connection.execute(
                """
                UPDATE sonder_launcher_operation_lock
                SET operation_id=NULL, owner_id=NULL, owner_pid=NULL,
                    owner_host=NULL, lease_until=NULL
                WHERE id=1 AND operation_id=? AND owner_id=?
                """,
                (operation_id, owner_id),
            )
        connection.commit()
        return updated == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def finish_operation(
    connect_fn: Callable[[], sqlite3.Connection],
    operation_id: str,
    owner_id: str,
    phase: str,
    message: str,
    last_error: str,
    commands: object,
    *,
    retention: int,
    max_operation_output: int,
    output_text_fn: Callable[..., str],
) -> bool:
    """Record terminal operation state, release the lock, and prune old terminal rows."""
    if phase not in TERMINAL_PHASES:
        raise ValueError("invalid terminal launcher phase")
    now = time.time()
    safe_commands = commands if isinstance(commands, list) else []
    commands_json = json.dumps(safe_commands, separators=(",", ":"))
    if len(commands_json) > max_operation_output:
        safe_commands = safe_commands[-2:]
        commands_json = json.dumps(safe_commands, separators=(",", ":"))[
            :max_operation_output
        ]
        try:
            json.loads(commands_json)
        except ValueError:
            commands_json = "[]"
    connection = connect_fn()
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE sonder_launcher_operations
            SET phase=?, updated_ts=?, finished_ts=?, message=?,
                last_error=?, commands_json=?
            WHERE id=? AND owner_id=? AND phase IN ('queued','running')
              AND EXISTS (
                  SELECT 1 FROM sonder_launcher_operation_lock
                  WHERE id=1 AND operation_id=? AND owner_id=?
              )
            """,
            (
                phase,
                now,
                now,
                output_text_fn(message),
                output_text_fn(last_error),
                commands_json,
                operation_id,
                owner_id,
                operation_id,
                owner_id,
            ),
        ).rowcount
        if updated == 1:
            connection.execute(
                """
                UPDATE sonder_launcher_operation_lock
                SET operation_id=NULL, owner_id=NULL, owner_pid=NULL,
                    owner_host=NULL, lease_until=NULL
                WHERE id=1 AND operation_id=? AND owner_id=?
                """,
                (operation_id, owner_id),
            )
        connection.execute(
            """
            DELETE FROM sonder_launcher_operations WHERE id IN (
                SELECT id FROM sonder_launcher_operations
                WHERE phase IN ('succeeded','failed','cancelled','interrupted')
                ORDER BY created_ts DESC LIMIT -1 OFFSET ?
            )
            """,
            (retention,),
        )
        connection.commit()
        return updated == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def build_operation_payload(
    status_payload: dict[str, Any],
    operation: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine controller status with operation metadata into the HTTP response shape."""
    phase = operation["phase"]
    status_payload.update({
        "ok": phase not in {"failed", "cancelled", "interrupted"},
        "operation_id": operation["id"],
        "operation_phase": phase,
        "operation": operation,
        "message": operation["message"],
        "last_error": operation["last_error"],
        "command": operation["command"],
        "commands": operation["commands"],
    })
    return status_payload
