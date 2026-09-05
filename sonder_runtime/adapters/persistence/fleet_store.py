"""Process-shared durable ledger for master/subagent orchestration.

Ownership and concurrency contract:

* This module is a service, not per-agent state. It owns the SQLite schema and
  short transactions; ``master_orchestrator`` owns execution threads/model calls.
* Every public operation is safe from any thread or local Sonder process.
* Queued -> running, finish, cancellation, and stale-owner reconciliation are
  conditional ``BEGIN IMMEDIATE`` transactions. Callers never write rows directly.
* The module is intentionally not hot-reloaded. Its database survives process
  replacement; callers may reload while continuing to use this stable API.
* Once stable principal authentication is supplied, ``owner_id`` is only the
  process lease/logging identity. It is retained for legacy callers and stale
  owner reconciliation, but it is not an authorization boundary.
"""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

import contextlib
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from sonder_runtime.domain.automation import state_machine as _sm
from sonder_runtime.platform import paths as _platform_paths


# SPEC-3 Phase 6: canonical fleet status classification in the domain layer.
ACTIVE_STATUSES = _sm.FLEET_ACTIVE
TERMINAL_STATUSES = _sm.FLEET_TERMINAL
DEFAULT_STALE_SECONDS = 60
DEFAULT_STALE_GRACE_SECONDS = 10
DEFAULT_FINISHED_RETENTION = 500
DEFAULT_EVENT_RETENTION = 2000
MAX_TASK_CHARS = 32_000
MAX_OUTPUT_CHARS = 128_000
MAX_ERROR_CHARS = 8_000
MAX_SUMMARY_CHARS = 2_000
MAX_ACTIVITY_CHARS = 500
MAX_MESSAGE_CHARS = 8_000
MAX_PENDING_MESSAGES_PER_AGENT = 32
MAX_PENDING_MESSAGES_PER_SCOPE = 128
MAX_MESSAGES_PER_RATE_WINDOW = 10
MESSAGE_RATE_WINDOW_SECONDS = 60
DEFAULT_MESSAGE_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MESSAGE_PENDING_TTL_SECONDS = 24 * 60 * 60
MESSAGE_MODES = frozenset(("follow_up", "steer"))
# Retry idempotency fence.  ``retried_by`` normally holds either '' or the
# agent id of a successful retry; while a retry is being dispatched it holds a
# short-lived ``retry-pending:<token>:<expiry>`` claim so two concurrent
# ``master_retry`` calls cannot both replay the same persisted master.  The
# pending claim only bridges the acquire-to-dispatch window: once the retry
# master row exists, the active-row check in :func:`acquire_retry_lease` is the
# durable fence, so an orphaned claim (process death mid-dispatch) simply
# expires instead of requiring reconciliation.
RETRY_LEASE_PREFIX = "retry-pending:"
DEFAULT_RETRY_LEASE_SECONDS = 120
MAX_RETRY_LEASE_SECONDS = 3600
RETRYABLE_STATUSES = frozenset(("interrupted", "failed", "cancelled", "task_drift"))

_SCHEMA_LOCK = threading.RLock()
# Cache the database identity, not merely its pathname.  A recovered or
# restored fleet ledger may replace the database while this process remains
# alive; its schema must be rechecked before it is used.
_INITIALIZED_PATHS: dict[str, tuple[int, int]] = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_owners (
    owner_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    host TEXT NOT NULL,
    started_ts REAL NOT NULL,
    heartbeat_ts REAL NOT NULL,
    stale_seen_ts REAL,
    closed_ts REAL
);
CREATE TABLE IF NOT EXISTS fleet_principals (
    principal_id TEXT PRIMARY KEY,
    secret_hash TEXT NOT NULL,
    created_ts REAL NOT NULL,
    last_seen_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fleet_agents (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    principal_id TEXT DEFAULT '',
    role TEXT NOT NULL,
    parent_id TEXT DEFAULT '',
    task TEXT DEFAULT '',
    status TEXT NOT NULL,
    activity TEXT DEFAULT '',
    started_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    finished_ts REAL,
    tool_calls INTEGER DEFAULT 0,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    files_json TEXT DEFAULT '[]',
    summary TEXT DEFAULT '',
    output TEXT DEFAULT '',
    error TEXT DEFAULT '',
    cancel_requested INTEGER DEFAULT 0,
    in_model_call INTEGER DEFAULT 0,
    requested_agents INTEGER DEFAULT 0,
    worker_slots INTEGER DEFAULT 0,
    mode TEXT DEFAULT '',
    tier TEXT DEFAULT '',
    project TEXT DEFAULT '',
    retry_of TEXT DEFAULT '',
    retried_by TEXT DEFAULT '',
    master_task_digest TEXT DEFAULT '',
    delegated_task_digest TEXT DEFAULT '',
    objective_ids_json TEXT DEFAULT '[]',
    task_drift INTEGER DEFAULT 0,
    drift_metrics_json TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_fleet_agents_status
    ON fleet_agents(status, updated_ts DESC);
CREATE INDEX IF NOT EXISTS idx_fleet_agents_parent
    ON fleet_agents(parent_id);
CREATE INDEX IF NOT EXISTS idx_fleet_agents_owner
    ON fleet_agents(owner_id, status);
CREATE TABLE IF NOT EXISTS fleet_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    ts REAL NOT NULL,
    stamp TEXT NOT NULL,
    message TEXT NOT NULL,
    master_task_digest TEXT DEFAULT '',
    delegated_task_digest TEXT DEFAULT '',
    objective_ids_json TEXT DEFAULT '[]',
    task_drift INTEGER DEFAULT 0,
    FOREIGN KEY(agent_id) REFERENCES fleet_agents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fleet_events_agent
    ON fleet_events(agent_id, event_id DESC);
CREATE TABLE IF NOT EXISTS fleet_messages (
    message_id TEXT PRIMARY KEY,
    sender_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    principal_id TEXT DEFAULT '',
    project_scope TEXT DEFAULT '',
    scope_root_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('follow_up', 'steer')),
    body TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'delivered', 'expired')),
    queued_ts REAL NOT NULL,
    delivered_ts REAL,
    expires_ts REAL NOT NULL,
    FOREIGN KEY(sender_id) REFERENCES fleet_agents(id) ON DELETE CASCADE,
    FOREIGN KEY(recipient_id) REFERENCES fleet_agents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fleet_messages_recipient
    ON fleet_messages(recipient_id, status, queued_ts);
CREATE INDEX IF NOT EXISTS idx_fleet_messages_scope
    ON fleet_messages(principal_id, project_scope, scope_root_id, status, queued_ts);
"""


def database_path() -> str:
    return _platform_paths.state_path("fleet.db", "SONDER_FLEET_DB")


def principal_credentials_path() -> str:
    return _platform_paths.state_path(
        "fleet-principal.json", "SONDER_FLEET_PRINCIPAL_FILE"
    )


def _principal_secret_hash(secret: str) -> str:
    return hashlib.sha256(str(secret).encode("utf-8")).hexdigest()


def register_principal(principal_id: str, secret: str) -> None:
    """Register or authenticate one stable local fleet authority."""
    principal = str(principal_id or "").strip()
    token = str(secret or "")
    if not principal.startswith("principal-") or len(principal) > 96:
        raise ValueError("invalid fleet principal id")
    if len(token) < 32 or len(token) > 256:
        raise ValueError("invalid fleet principal secret")
    digest = _principal_secret_hash(token)
    now = time.time()
    with _write_transaction() as conn:
        row = conn.execute(
            "SELECT secret_hash FROM fleet_principals WHERE principal_id=?",
            (principal,),
        ).fetchone()
        if row is not None and not secrets.compare_digest(row["secret_hash"], digest):
            raise PermissionError("fleet principal authentication failed")
        conn.execute(
            """
            INSERT INTO fleet_principals(
                principal_id, secret_hash, created_ts, last_seen_ts
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(principal_id) DO UPDATE SET last_seen_ts=excluded.last_seen_ts
            """,
            (principal, digest, now, now),
        )


def _authenticate_principal(
    conn: sqlite3.Connection, principal_id: str, secret: str,
) -> str:
    principal = str(principal_id or "").strip()
    token = str(secret or "")
    row = conn.execute(
        "SELECT secret_hash FROM fleet_principals WHERE principal_id=?",
        (principal,),
    ).fetchone()
    if (
        row is None
        or not token
        or not secrets.compare_digest(
            str(row["secret_hash"]), _principal_secret_hash(token)
        )
    ):
        raise PermissionError("fleet principal authentication failed")
    return principal


def local_principal_credentials() -> tuple[str, str]:
    """Load or atomically publish the per-user stable fleet credential."""
    path = Path(principal_credentials_path()).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            principal = str(payload.get("principal_id") or "")
            secret = str(payload.get("secret") or "")
            register_principal(principal, secret)
            return principal, secret
        except FileNotFoundError:
            payload = {
                "principal_id": "principal-%s" % uuid.uuid4().hex,
                "secret": secrets.token_hex(32),
            }
            temporary = path.with_name(
                ".%s.%s.tmp" % (path.name, uuid.uuid4().hex)
            )
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if os.name != "nt":
                    with contextlib.suppress(OSError):
                        os.chmod(temporary, 0o600)
                # A hard-link publishes a fully-written inode atomically while
                # failing if another process won the destination name. Unlike
                # os.replace(), it cannot clobber an established credential.
                os.link(temporary, path)
            except FileExistsError:
                continue
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
            register_principal(payload["principal_id"], payload["secret"])
            return payload["principal_id"], payload["secret"]
        except PermissionError:
            raise
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            raise RuntimeError("invalid fleet principal credential file") from exc
    raise RuntimeError("could not establish fleet principal credentials")


def _clamp_text(value, limit: int) -> str:
    text = str(value or "")
    return text[:limit]


def _files_json(value) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            parsed = [value]
    else:
        parsed = value or []
    if not isinstance(parsed, list):
        parsed = [str(parsed)]
    return json.dumps([_clamp_text(item, 2000) for item in parsed[:100]])


def _objective_ids_json(value) -> str:
    values = value if isinstance(value, (list, tuple)) else []
    return json.dumps(
        [_clamp_text(item, 64) for item in values[:32]],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _drift_metrics_json(value) -> str:
    metrics = value if isinstance(value, dict) else {}
    encoded = json.dumps(
        metrics, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) <= 4000:
        return encoded
    return json.dumps({
        "audit_truncated": True,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }, sort_keys=True, separators=(",", ":"))


def _database_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def _ensure_schema(path: str) -> None:
    resolved = str(Path(path).expanduser().resolve())
    resolved_path = Path(resolved)
    with _SCHEMA_LOCK:
        identity = _database_identity(resolved_path)
        if identity is not None and _INITIALIZED_PATHS.get(resolved) == identity:
            return
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        last_error = None
        for attempt in range(4):
            conn = None
            try:
                conn = owned_sqlite_connect(resolved, timeout=5)
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(fleet_agents)"
                    ).fetchall()
                }
                agent_columns = {
                    "retry_of": "TEXT DEFAULT ''",
                    "retried_by": "TEXT DEFAULT ''",
                    "project": "TEXT DEFAULT ''",
                    "master_task_digest": "TEXT DEFAULT ''",
                    "delegated_task_digest": "TEXT DEFAULT ''",
                    "objective_ids_json": "TEXT DEFAULT '[]'",
                    "task_drift": "INTEGER DEFAULT 0",
                    "drift_metrics_json": "TEXT DEFAULT '{}'",
                }
                for name, definition in agent_columns.items():
                    if name in columns:
                        continue
                    try:
                        conn.execute(
                            "ALTER TABLE fleet_agents ADD COLUMN %s %s"
                            % (name, definition)
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                if "principal_id" not in columns:
                    try:
                        conn.execute(
                            "ALTER TABLE fleet_agents ADD COLUMN principal_id TEXT DEFAULT ''"
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                event_columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(fleet_events)"
                    ).fetchall()
                }
                for name, definition in {
                    "master_task_digest": "TEXT DEFAULT ''",
                    "delegated_task_digest": "TEXT DEFAULT ''",
                    "objective_ids_json": "TEXT DEFAULT '[]'",
                    "task_drift": "INTEGER DEFAULT 0",
                }.items():
                    if name not in event_columns:
                        try:
                            conn.execute(
                                "ALTER TABLE fleet_events ADD COLUMN %s %s"
                                % (name, definition)
                            )
                        except sqlite3.OperationalError as exc:
                            if "duplicate column" not in str(exc).lower():
                                raise
                conn.commit()
                if os.name != "nt":
                    with contextlib.suppress(OSError):
                        os.chmod(resolved, 0o600)
                identity = _database_identity(resolved_path)
                if identity is None:  # pragma: no cover - SQLite just committed it
                    raise RuntimeError("fleet database disappeared during schema setup")
                _INITIALIZED_PATHS[resolved] = identity
                return
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc).lower() or attempt == 3:
                    raise
                time.sleep(0.05 * (attempt + 1))
            finally:
                if conn is not None:
                    conn.close()
        if last_error is not None:
            raise last_error


def _connect() -> sqlite3.Connection:
    path = database_path()
    _ensure_schema(path)
    conn = owned_sqlite_connect(path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextlib.contextmanager
def _write_transaction():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_dict(row) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    try:
        data["files"] = json.loads(data.pop("files_json", "[]") or "[]")
    except (TypeError, ValueError):
        data["files"] = []
    data["cancel_requested"] = bool(data.get("cancel_requested"))
    data["in_model_call"] = bool(data.get("in_model_call"))
    data["task_drift"] = bool(data.get("task_drift"))
    for column, public_name, fallback in (
        ("objective_ids_json", "objective_ids", []),
        ("drift_metrics_json", "drift_metrics", {}),
    ):
        try:
            data[public_name] = json.loads(data.pop(column, "") or json.dumps(fallback))
        except (TypeError, ValueError):
            data[public_name] = fallback
        if not isinstance(data[public_name], type(fallback)):
            data[public_name] = fallback
    return data


def _event_dict(row) -> dict:
    data = dict(row)
    try:
        data["objective_ids"] = json.loads(data.pop("objective_ids_json", "[]") or "[]")
    except (TypeError, ValueError):
        data["objective_ids"] = []
    if not isinstance(data["objective_ids"], list):
        data["objective_ids"] = []
    data["task_drift"] = bool(data.get("task_drift"))
    return data


def register_owner(owner_id: str, pid: int, started_ts: float | None = None) -> None:
    now = time.time()
    started = float(started_ts or now)
    with _write_transaction() as conn:
        conn.execute(
            """
            INSERT INTO fleet_owners(
                owner_id, pid, host, started_ts, heartbeat_ts, stale_seen_ts, closed_ts
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(owner_id) DO UPDATE SET
                pid=excluded.pid,
                host=excluded.host,
                heartbeat_ts=excluded.heartbeat_ts,
                stale_seen_ts=NULL,
                closed_ts=NULL
            """,
            (owner_id, int(pid), socket.gethostname(), started, now),
        )


def heartbeat_owner(owner_id: str) -> bool:
    now = time.time()
    with _write_transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE fleet_owners
            SET heartbeat_ts=?, stale_seen_ts=NULL
            WHERE owner_id=? AND closed_ts IS NULL
            """,
            (now, owner_id),
        )
        return cursor.rowcount > 0


def close_owner(owner_id: str, reason: str = "process exited before completion") -> int:
    # State machine is the single source of truth for valid transitions.
    for src in ("queued", "running"):
        if not _sm.fleet_can_transition(src, "interrupted"):
            raise ValueError(
                "fleet state machine does not allow %s -> interrupted" % src
            )
    now = time.time()
    with _write_transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE fleet_agents
            SET status='interrupted', activity=?, cancel_requested=1,
                in_model_call=0, finished_ts=?, updated_ts=?
            WHERE owner_id=? AND status IN ('queued', 'running')
            """,
            (_clamp_text(reason, MAX_ACTIVITY_CHARS), now, now, owner_id),
        )
        conn.execute(
            "UPDATE fleet_owners SET closed_ts=?, heartbeat_ts=? WHERE owner_id=?",
            (now, now, owner_id),
        )
        return int(cursor.rowcount)


def reconcile_stale_owners(
    *, now: float | None = None, stale_seconds: int = DEFAULT_STALE_SECONDS,
    grace_seconds: int = DEFAULT_STALE_GRACE_SECONDS,
) -> dict:
    # State machine is the single source of truth for valid transitions.
    for src in ("queued", "running"):
        if not _sm.fleet_can_transition(src, "interrupted"):
            raise ValueError(
                "fleet state machine does not allow %s -> interrupted" % src
            )
    current = float(now or time.time())
    stale_seconds = max(15, min(int(stale_seconds), 3600))
    grace_seconds = max(1, min(int(grace_seconds), 300))
    cutoff = current - stale_seconds
    probe = _connect()
    try:
        has_stale_owner = probe.execute(
            """
            SELECT 1 FROM fleet_owners
            WHERE closed_ts IS NULL AND heartbeat_ts < ? LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
    finally:
        probe.close()
    if not has_stale_owner:
        return {"suspect_owners": 0, "interrupted": 0, "owners": []}
    suspects = 0
    interrupted = 0
    owners = []
    with _write_transaction() as conn:
        rows = conn.execute(
            """
            SELECT owner_id, stale_seen_ts
            FROM fleet_owners
            WHERE closed_ts IS NULL AND heartbeat_ts < ?
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            owner_id = row["owner_id"]
            stale_seen = row["stale_seen_ts"]
            if stale_seen is None:
                conn.execute(
                    "UPDATE fleet_owners SET stale_seen_ts=? WHERE owner_id=?",
                    (current, owner_id),
                )
                suspects += 1
                continue
            if float(stale_seen) > current - grace_seconds:
                suspects += 1
                continue
            cursor = conn.execute(
                """
                UPDATE fleet_agents
                SET status='interrupted',
                    activity='interrupted after owner heartbeat expired',
                    cancel_requested=1, in_model_call=0,
                    finished_ts=?, updated_ts=?
                WHERE owner_id=? AND status IN ('queued', 'running')
                """,
                (current, current, owner_id),
            )
            interrupted += int(cursor.rowcount)
            owners.append(owner_id)
            conn.execute(
                "UPDATE fleet_owners SET closed_ts=? WHERE owner_id=?",
                (current, owner_id),
            )
    return {"suspect_owners": suspects, "interrupted": interrupted, "owners": owners}


def create_agent(
    row: dict, owner_id: str, owner_pid: int, *, principal_id: str = "",
    principal_secret: str = "",
) -> dict:
    now = float(row.get("updated_ts") or time.time())
    status = str(row.get("status") or "queued")
    activity = _clamp_text(row.get("activity") or status, MAX_ACTIVITY_CHARS)
    summary = _clamp_text(row.get("summary"), MAX_SUMMARY_CHARS)
    cancel_requested = bool(row.get("cancel_requested"))
    finished_ts = row.get("finished_ts")
    principal = str(principal_id or row.get("principal_id") or "")
    with _write_transaction() as conn:
        if principal:
            _authenticate_principal(conn, principal, principal_secret)
        parent_id = str(row.get("parent_id") or "")
        if parent_id:
            parent = conn.execute(
                "SELECT status, cancel_requested, principal_id, project "
                "FROM fleet_agents WHERE id=?",
                (parent_id,),
            ).fetchone()
            if parent:
                parent_principal = str(parent["principal_id"] or "")
                if parent_principal != principal:
                    raise PermissionError("child principal must match its parent tree")
                if str(parent["project"] or "") != str(row.get("project") or ""):
                    raise PermissionError("child project must match its parent tree")
            inherited = bool(
                parent and (
                    parent["cancel_requested"]
                    or parent["status"] in ("cancelled", "interrupted")
                )
            )
            if inherited:
                status = "cancelled"
                activity = "cancelled with parent"
                summary = "cancelled before model call"
                cancel_requested = True
                finished_ts = now
        conn.execute(
            """
            INSERT INTO fleet_agents(
                id, owner_id, owner_pid, principal_id, role, parent_id, task, status,
                activity, started_ts, updated_ts, finished_ts, tool_calls,
                tokens_in, tokens_out, files_json, summary, output, error,
                cancel_requested, in_model_call, requested_agents,
                worker_slots, mode, tier, project, retry_of, retried_by,
                master_task_digest, delegated_task_digest, objective_ids_json,
                task_drift, drift_metrics_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                row["id"], owner_id, int(owner_pid), principal, row.get("role", "agent"),
                parent_id, _clamp_text(row.get("task"), MAX_TASK_CHARS), status,
                activity, float(row.get("started_ts") or now), now, finished_ts,
                int(row.get("tool_calls") or 0), int(row.get("tokens_in") or 0),
                int(row.get("tokens_out") or 0), _files_json(row.get("files")),
                summary, _clamp_text(row.get("output"), MAX_OUTPUT_CHARS),
                _clamp_text(row.get("error"), MAX_ERROR_CHARS),
                int(cancel_requested), int(bool(row.get("in_model_call"))),
                int(row.get("requested_agents") or 0),
                int(row.get("worker_slots") or 0), str(row.get("mode") or ""),
                str(row.get("tier") or ""), str(row.get("project") or ""),
                str(row.get("retry_of") or ""),
                str(row.get("retried_by") or ""),
                _clamp_text(row.get("master_task_digest"), 64),
                _clamp_text(row.get("delegated_task_digest"), 64),
                _objective_ids_json(row.get("objective_ids")),
                int(bool(row.get("task_drift"))),
                _drift_metrics_json(row.get("drift_metrics")),
            ),
        )
        stored = conn.execute(
            "SELECT * FROM fleet_agents WHERE id=?", (row["id"],)
        ).fetchone()
    return _row_dict(stored)


def start_agent(
    agent_id: str, owner_id: str, activity: str, *, in_model_call: bool = False,
    tool_calls: int = 0, requested_agents: int = 0, worker_slots: int = 0,
    mode: str = "", tier: str = "",
) -> dict | None:
    # State machine is the single source of truth: queued -> running.
    if not _sm.fleet_can_transition("queued", "running"):
        raise ValueError(
            "fleet state machine does not allow queued -> running"
        )
    now = time.time()
    with _write_transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE fleet_agents
            SET status='running', activity=?, in_model_call=?, tool_calls=?,
                requested_agents=CASE WHEN ? > 0 THEN ? ELSE requested_agents END,
                worker_slots=CASE WHEN ? > 0 THEN ? ELSE worker_slots END,
                mode=CASE WHEN ? <> '' THEN ? ELSE mode END,
                tier=CASE WHEN ? <> '' THEN ? ELSE tier END,
                updated_ts=?
            WHERE id=? AND owner_id=? AND status='queued' AND cancel_requested=0
            """,
            (
                _clamp_text(activity, MAX_ACTIVITY_CHARS), int(in_model_call),
                int(tool_calls), int(requested_agents), int(requested_agents),
                int(worker_slots), int(worker_slots), mode, mode, tier, tier, now,
                agent_id, owner_id,
            ),
        )
        if cursor.rowcount <= 0:
            return None
        row = conn.execute(
            "SELECT * FROM fleet_agents WHERE id=?", (agent_id,)
        ).fetchone()
    return _row_dict(row)


def begin_model_call(
    agent_id: str, owner_id: str, activity: str, *, tool_calls: int,
) -> dict | None:
    now = time.time()
    with _write_transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE fleet_agents
            SET activity=?, in_model_call=1, tool_calls=?, updated_ts=?
            WHERE id=? AND owner_id=? AND status='running' AND cancel_requested=0
            """,
            (
                _clamp_text(activity, MAX_ACTIVITY_CHARS), int(tool_calls), now,
                agent_id, owner_id,
            ),
        )
        if cursor.rowcount <= 0:
            return None
        row = conn.execute(
            "SELECT * FROM fleet_agents WHERE id=?", (agent_id,)
        ).fetchone()
    return _row_dict(row)


def update_agent(agent_id: str, owner_id: str, **changes) -> dict | None:
    allowed = {
        "activity", "tool_calls", "tokens_in", "tokens_out", "files",
        "summary", "requested_agents", "worker_slots", "mode", "tier",
        "in_model_call",
    }
    values = []
    assignments = []
    for key, value in changes.items():
        if key not in allowed:
            continue
        column = "files_json" if key == "files" else key
        if key == "files":
            value = _files_json(value)
        elif key == "activity":
            value = _clamp_text(value, MAX_ACTIVITY_CHARS)
        elif key == "summary":
            value = _clamp_text(value, MAX_SUMMARY_CHARS)
        elif key == "in_model_call":
            value = int(bool(value))
        assignments.append(f"{column}=?")
        values.append(value)
    if not assignments:
        return get_agent(agent_id)
    assignments.append("updated_ts=?")
    values.append(time.time())
    values.extend([agent_id, owner_id])
    with _write_transaction() as conn:
        conn.execute(
            "UPDATE fleet_agents SET %s WHERE id=? AND owner_id=?" % ", ".join(assignments),
            values,
        )
        row = conn.execute(
            "SELECT * FROM fleet_agents WHERE id=?", (agent_id,)
        ).fetchone()
    return _row_dict(row)


def transition_provenance(
    current_status: str, target_status: str, *, source: str = "process",
) -> dict:
    """Build a provenance record for a state transition.

    The record carries enough context to audit who caused a transition and
    whether it was authoritative (process lifecycle, cancellation protocol)
    or inferred (stale-owner reconciliation, retry dispatch).
    """
    return {
        "from": current_status,
        "to": target_status,
        "source": source,
        "ts": time.time(),
        "valid": _sm.fleet_can_transition(current_status, target_status),
    }


def finish_agent(
    agent_id: str, owner_id: str, *, output: str = "", error: str = "",
    task_drift: bool = False, drift_metrics: dict | None = None,
) -> tuple[dict | None, str]:
    now = time.time()
    with _write_transaction() as conn:
        row = conn.execute(
            "SELECT * FROM fleet_agents WHERE id=? AND owner_id=?",
            (agent_id, owner_id),
        ).fetchone()
        if row is None:
            return None, ""
        current = _row_dict(row)
        if current["status"] == "interrupted":
            return current, "INTERRUPTED"
        if current["cancel_requested"] or current["status"] == "cancelled":
            status = "cancelled"
            activity = "cancelled; late result discarded"
            summary = "cancelled; active call returned and its result was discarded"
            final_output = ""
            final_error = ""
            marker = "CANCELLED"
            tokens_out = 0
        elif current["status"] in ("done", "failed", "task_drift"):
            marker = current.get("output") or (
                "ERROR: %s" % current.get("error") if current.get("error") else ""
            )
            return current, marker
        elif task_drift:
            status = "task_drift"
            activity = "task drift; result rejected before aggregation"
            summary = "task drift; authoritative objective coverage failed"
            final_output = ""
            final_error = _clamp_text(error or summary, MAX_ERROR_CHARS)
            marker = "TASK_DRIFT"
            tokens_out = 0
        else:
            status = "failed" if error else "done"
            activity = "failed: %s" % _clamp_text(error, 160) if error else "finished"
            summary = _clamp_text(output or error, MAX_SUMMARY_CHARS)
            final_output = _clamp_text(output, MAX_OUTPUT_CHARS)
            final_error = _clamp_text(error, MAX_ERROR_CHARS)
            marker = output
            tokens_out = max(0, (len(output or "") + 3) // 4)
        # State machine is the single source of truth for valid transitions.
        if current["status"] != status and not _sm.fleet_can_transition(
            current["status"], status
        ):
            raise ValueError(
                "fleet state machine does not allow %s -> %s"
                % (current["status"], status)
            )
        conn.execute(
            """
            UPDATE fleet_agents
            SET status=?, activity=?, summary=?, output=?, error=?,
                tokens_out=?, in_model_call=0, finished_ts=?, updated_ts=?,
                task_drift=?, drift_metrics_json=?
            WHERE id=? AND owner_id=?
            """,
            (
                status, activity, summary, final_output, final_error, tokens_out,
                now, now, int(bool(task_drift)),
                _drift_metrics_json(drift_metrics),
                agent_id, owner_id,
            ),
        )
        # Steering is meaningful only while an agent can reach a cooperative
        # checkpoint. Follow-ups remain retained for an explicit resumed turn.
        conn.execute(
            "UPDATE fleet_messages SET status='expired' "
            "WHERE recipient_id=? AND mode='steer' AND status='queued'",
            (agent_id,),
        )
        stored = conn.execute(
            "SELECT * FROM fleet_agents WHERE id=?", (agent_id,)
        ).fetchone()
        stored_dict = _row_dict(stored)
        if (
            stored_dict
            and stored_dict.get("role") == "master"
            and stored_dict.get("retry_of")
            and stored_dict.get("status") in ("failed", "cancelled", "task_drift")
        ):
            # The retry attempt itself ended without success.  Clear any
            # pending dispatch claim on the source promptly so an operator can
            # retry again without waiting out the claim TTL.  This transaction
            # also removes the retry master from the active set, so no other
            # dispatcher can hold a live claim at this moment.
            conn.execute(
                "UPDATE fleet_agents SET retried_by='', updated_ts=? "
                "WHERE id=? AND retried_by LIKE ? ESCAPE '\\'",
                (
                    now, stored_dict["retry_of"],
                    RETRY_LEASE_PREFIX.replace("_", "\\_") + "%",
                ),
            )
        if (
            stored_dict
            and stored_dict.get("role") == "master"
            and stored_dict.get("status") == "done"
            and stored_dict.get("retry_of")
        ):
            # State machine is the single source of truth for retry-source
            # transitions.
            for src in ("interrupted", "failed", "cancelled", "task_drift"):
                if not _sm.fleet_can_transition(src, "retried"):
                    raise ValueError(
                        "fleet state machine does not allow %s -> retried"
                        % src
                    )
            conn.execute(
                """
                UPDATE fleet_agents
                SET status='retried', retried_by=?,
                    activity=?, updated_ts=?
                WHERE id=? AND status IN ('interrupted', 'failed', 'cancelled', 'task_drift')
                """,
                (
                    agent_id, "retried successfully as %s" % agent_id,
                    now, stored_dict["retry_of"],
                ),
            )
    return _row_dict(stored), marker


def cancel_agents(selector: str) -> dict:
    # State machine is the single source of truth for valid transitions.
    if not _sm.fleet_can_transition("queued", "cancelled"):
        raise ValueError(
            "fleet state machine does not allow queued -> cancelled"
        )
    value = str(selector or "").strip()
    now = time.time()
    with _write_transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM fleet_agents WHERE status IN ('queued', 'running')"
        ).fetchall()
        active = {row["id"]: dict(row) for row in rows}
        if value.lower() in ("all", "*"):
            selected = set(active)
        elif not value:
            selected = set()
        elif value in active:
            selected = {value}
        else:
            selected = {agent_id for agent_id in active if agent_id.startswith(value)}
        changed = True
        while changed:
            changed = False
            for agent_id, row in active.items():
                if row.get("parent_id") in selected and agent_id not in selected:
                    selected.add(agent_id)
                    changed = True
        queued = 0
        running = 0
        model_calls = 0
        for agent_id in sorted(selected):
            row = active[agent_id]
            if row["status"] == "queued":
                queued += 1
                conn.execute(
                    """
                    UPDATE fleet_agents
                    SET status='cancelled', cancel_requested=1,
                        activity='cancelled before start',
                        summary='cancelled before model call',
                        in_model_call=0, finished_ts=?, updated_ts=?
                    WHERE id=? AND status='queued'
                    """,
                    (now, now, agent_id),
                )
            else:
                running += 1
                in_call = bool(row.get("in_model_call"))
                model_calls += int(in_call)
                activity = (
                    "cancellation requested; waiting for active model call"
                    if in_call else
                    "cancellation requested; stopping after active children"
                )
                conn.execute(
                    """
                    UPDATE fleet_agents
                    SET cancel_requested=1, activity=?, updated_ts=?
                    WHERE id=? AND status='running'
                    """,
                    (activity, now, agent_id),
                )
        selected_rows = []
        if selected:
            placeholders = ",".join("?" for _ in selected)
            selected_rows = conn.execute(
                f"SELECT * FROM fleet_agents WHERE id IN ({placeholders})",
                sorted(selected),
            ).fetchall()
    return {
        "selector": value,
        "matched": len(selected),
        "running": running,
        "queued": queued,
        "model_calls": model_calls,
        "agent_ids": sorted(selected),
        "agents": [_row_dict(row) for row in selected_rows],
        "cooperative": True,
    }


def cancellation_requested(agent_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT cancel_requested, status FROM fleet_agents WHERE id=?",
            (agent_id,),
        ).fetchone()
        return bool(
            row and (
                row["cancel_requested"]
                or row["status"] in ("cancelled", "interrupted")
            )
        )
    finally:
        conn.close()


def _retry_lease_expiry(value: str) -> float | None:
    """Parse the expiry of a pending retry claim, or ``None`` for non-claims."""
    text = str(value or "")
    if not text.startswith(RETRY_LEASE_PREFIX):
        return None
    parts = text.split(":")
    if len(parts) != 3:
        return 0.0  # malformed claim: treat as immediately expired, never valid
    try:
        return float(parts[2])
    except (TypeError, ValueError):
        return 0.0


def acquire_retry_lease(
    agent_id: str, *, lease_seconds: int = DEFAULT_RETRY_LEASE_SECONDS,
    now: float | None = None,
) -> dict | None:
    """Atomically claim the right to dispatch one retry of a persisted master.

    Returns ``None`` when the master is missing, not in a retryable terminal
    state, already successfully retried, currently claimed by an unexpired
    pending lease, or already has an active (queued/running) retry master.
    The returned token must be released with :func:`release_retry_lease` after
    dispatch; once the retry master row exists it carries the fence itself.
    """
    target = str(agent_id or "").strip()
    if not target:
        return None
    current = float(now if now is not None else time.time())
    ttl = max(5, min(int(lease_seconds), MAX_RETRY_LEASE_SECONDS))
    with _write_transaction() as conn:
        row = conn.execute(
            "SELECT id, role, status, retried_by FROM fleet_agents WHERE id=?",
            (target,),
        ).fetchone()
        if row is None or row["role"] != "master":
            return None
        if row["status"] not in RETRYABLE_STATUSES:
            return None
        expiry = _retry_lease_expiry(row["retried_by"])
        if expiry is None and str(row["retried_by"] or ""):
            # A non-claim value means a recorded successful retry; the status
            # check above should already exclude this, but fail closed anyway.
            return None
        if expiry is not None and expiry > current:
            return None
        active = conn.execute(
            "SELECT 1 FROM fleet_agents WHERE retry_of=? AND role='master' "
            "AND status IN ('queued', 'running') LIMIT 1",
            (target,),
        ).fetchone()
        if active is not None:
            return None
        token = uuid.uuid4().hex
        expires_ts = current + ttl
        claim = "%s%s:%.3f" % (RETRY_LEASE_PREFIX, token, expires_ts)
        changed = conn.execute(
            "UPDATE fleet_agents SET retried_by=?, updated_ts=? "
            "WHERE id=? AND retried_by=?",
            (claim, current, target, str(row["retried_by"] or "")),
        ).rowcount
        if not changed:
            return None
        return {"agent_id": target, "token": token, "expires_ts": expires_ts}


def release_retry_lease(agent_id: str, token: str) -> bool:
    """Release one pending retry claim; never touches a recorded retry id."""
    target = str(agent_id or "").strip()
    value = str(token or "").strip()
    if not target or not value:
        return False
    with _write_transaction() as conn:
        return conn.execute(
            "UPDATE fleet_agents SET retried_by='', updated_ts=? "
            "WHERE id=? AND retried_by LIKE ? ESCAPE '\\'",
            (
                time.time(), target,
                RETRY_LEASE_PREFIX.replace("_", "\\_") + value + ":%",
            ),
        ).rowcount > 0


def get_agent(selector: str, *, role: str = "") -> dict | None:
    value = str(selector or "").strip()
    if not value:
        return None
    conn = _connect()
    try:
        params = [value]
        role_clause = ""
        if role:
            role_clause = " AND role=?"
            params.append(role)
        row = conn.execute(
            "SELECT * FROM fleet_agents WHERE id=?" + role_clause,
            params,
        ).fetchone()
        if row is not None:
            return _row_dict(row)
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params = [escaped + "%"]
        if role:
            params.append(role)
        rows = conn.execute(
            "SELECT * FROM fleet_agents WHERE id LIKE ? ESCAPE '\\'" + role_clause
            + " ORDER BY updated_ts DESC LIMIT 2",
            params,
        ).fetchall()
        return _row_dict(rows[0]) if len(rows) == 1 else None
    finally:
        conn.close()


def _message_dict(row) -> dict | None:
    if row is None:
        return None
    return dict(row)


def _scope_root_id(conn: sqlite3.Connection, agent_id: str) -> str:
    """Return the persisted root of one agent tree, rejecting corrupt cycles."""
    current = str(agent_id or "")
    seen = set()
    for _ in range(64):
        if not current or current in seen:
            raise ValueError("agent parent chain is invalid")
        seen.add(current)
        row = conn.execute(
            "SELECT id, parent_id FROM fleet_agents WHERE id=?", (current,)
        ).fetchone()
        if row is None:
            raise ValueError("agent does not exist")
        parent_id = str(row["parent_id"] or "")
        if not parent_id:
            return str(row["id"])
        current = parent_id
    raise ValueError("agent parent chain exceeds safety limit")


def list_agents_scoped(
    owner_id: str = "", *, project: str = "", parent_id: str = "",
    include_finished: bool = True, limit: int = 50, principal_id: str = "",
    principal_secret: str = "",
) -> list[dict]:
    """Discover retained agents without crossing owner or project boundaries.

    ``project`` is matched exactly. An empty project therefore means the
    explicitly unscoped fleet, not every project owned by the caller.
    ``parent_id`` selects one persisted agent tree and must itself be in the
    requested owner/project scope.
    """
    owner = str(owner_id or "").strip()
    if not owner and not principal_id:
        raise ValueError("owner_id or authenticated principal is required")
    scoped_project = str(project or "")
    capped_limit = max(1, min(int(limit or 50), 200))
    conn = _connect()
    try:
        principal = ""
        if principal_id:
            principal = _authenticate_principal(
                conn, principal_id, principal_secret,
            )
        scope_column = "principal_id" if principal else "owner_id"
        scope_value = principal or owner
        subtree_id = ""
        if parent_id:
            parent = conn.execute(
                "SELECT owner_id, principal_id, project FROM fleet_agents WHERE id=?",
                (str(parent_id),),
            ).fetchone()
            if (
                parent is None
                or str(parent[scope_column] or "") != scope_value
                or str(parent["project"] or "") != scoped_project
            ):
                raise PermissionError("parent agent is outside the requested scope")
            # Discovery anchored at a child returns that child and descendants,
            # not siblings elsewhere in the same authorized root tree.
            subtree_id = str(parent_id)
        if subtree_id:
            status_clause = "" if include_finished else (
                " AND a.status IN ('queued', 'running')"
            )
            rows = conn.execute(
                """
                WITH RECURSIVE scoped(id) AS (
                    SELECT ?
                    UNION ALL
                    SELECT child.id FROM fleet_agents AS child
                    JOIN scoped ON child.parent_id=scoped.id
                    WHERE child.%s=? AND child.project=?
                )
                SELECT a.* FROM fleet_agents AS a
                JOIN scoped ON scoped.id=a.id
                WHERE a.%s=? AND a.project=?
                """ % (scope_column, scope_column)
                + status_clause + " ORDER BY a.updated_ts DESC LIMIT ?",
                (
                    subtree_id, scope_value, scoped_project, scope_value, scoped_project,
                    capped_limit,
                ),
            ).fetchall()
        else:
            status_clause = "" if include_finished else (
                " AND status IN ('queued', 'running')"
            )
            rows = conn.execute(
                "SELECT * FROM fleet_agents WHERE %s=? AND project=?" % scope_column
                + status_clause + " ORDER BY updated_ts DESC LIMIT ?",
                (scope_value, scoped_project, capped_limit),
            ).fetchall()
        return [_row_dict(row) for row in rows]
    finally:
        conn.close()


def queue_agent_message(
    sender_id: str, recipient_id: str, owner_id: str, *, project: str = "",
    mode: str, body: str, now: float | None = None,
    pending_ttl_seconds: int = DEFAULT_MESSAGE_PENDING_TTL_SECONDS,
    principal_id: str = "", principal_secret: str = "",
) -> dict:
    """Queue a bounded message inside one retained agent tree.

    ``steer`` is accepted only while the recipient is queued/running and is a
    cooperative instruction for its next safe checkpoint. ``follow_up`` is a
    separate retained turn request and may target active or terminal agents.
    Delivery means a consumer atomically claimed the message; it does not imply
    that a model accepted or completed the instruction.
    """
    sender = str(sender_id or "").strip()
    recipient = str(recipient_id or "").strip()
    owner = str(owner_id or "").strip()
    scoped_project = str(project or "")
    message_mode = str(mode or "").strip().lower().replace("-", "_")
    message_body = str(body or "").strip()
    if not sender or not recipient or not owner:
        raise ValueError("sender_id, recipient_id, and owner_id are required")
    if message_mode not in MESSAGE_MODES:
        raise ValueError("mode must be follow_up or steer")
    if not message_body:
        raise ValueError("message body is required")
    if len(message_body) > MAX_MESSAGE_CHARS:
        raise ValueError("message body exceeds %s characters" % MAX_MESSAGE_CHARS)
    current = float(now if now is not None else time.time())
    ttl = max(60, min(int(pending_ttl_seconds), 7 * 24 * 60 * 60))
    with _write_transaction() as conn:
        principal = ""
        if principal_id:
            principal = _authenticate_principal(
                conn, principal_id, principal_secret,
            )
        rows = conn.execute(
            "SELECT id, owner_id, principal_id, project, status FROM fleet_agents "
            "WHERE id IN (?, ?)",
            (sender, recipient),
        ).fetchall()
        agents = {str(row["id"]): row for row in rows}
        if sender not in agents or recipient not in agents:
            raise ValueError("sender or recipient agent does not exist")
        for row in agents.values():
            if (
                (
                    str(row["principal_id"] or "") != principal
                    if principal else row["owner_id"] != owner
                )
                or str(row["project"] or "") != scoped_project
            ):
                raise PermissionError("agent is outside the requested owner/project scope")
        sender_root = _scope_root_id(conn, sender)
        recipient_root = _scope_root_id(conn, recipient)
        if sender_root != recipient_root:
            raise PermissionError("agents do not belong to the same parent scope")
        if message_mode == "steer" and agents[recipient]["status"] not in ACTIVE_STATUSES:
            raise ValueError("steer requires a queued or running recipient")
        conn.execute(
            "UPDATE fleet_messages SET status='expired' "
            "WHERE status='queued' AND expires_ts<=?", (current,)
        )
        recipient_pending = conn.execute(
            "SELECT COUNT(*) FROM fleet_messages "
            "WHERE recipient_id=? AND status='queued'",
            (recipient,),
        ).fetchone()[0]
        if int(recipient_pending) >= MAX_PENDING_MESSAGES_PER_AGENT:
            raise RuntimeError("recipient pending-message limit reached")
        message_scope_column = "principal_id" if principal else "owner_id"
        scope_pending = conn.execute(
            "SELECT COUNT(*) FROM fleet_messages WHERE %s=? AND project_scope=? "
            "AND scope_root_id=? AND status='queued'" % message_scope_column,
            (principal or owner, scoped_project, sender_root),
        ).fetchone()[0]
        if int(scope_pending) >= MAX_PENDING_MESSAGES_PER_SCOPE:
            raise RuntimeError("parent scope pending-message limit reached")
        recent = conn.execute(
            "SELECT COUNT(*) FROM fleet_messages WHERE sender_id=? AND queued_ts>?",
            (sender, current - MESSAGE_RATE_WINDOW_SECONDS),
        ).fetchone()[0]
        if int(recent) >= MAX_MESSAGES_PER_RATE_WINDOW:
            raise RuntimeError("sender message rate limit reached")
        message_id = "msg-%s" % uuid.uuid4().hex
        expires = current + ttl
        conn.execute(
            """
            INSERT INTO fleet_messages(
                message_id, sender_id, recipient_id, owner_id, principal_id, project_scope,
                scope_root_id, mode, body, status, queued_ts, delivered_ts,
                expires_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, NULL, ?)
            """,
            (
                message_id, sender, recipient, owner, principal, scoped_project,
                sender_root, message_mode, message_body, current, expires,
            ),
        )
        row = conn.execute(
            "SELECT * FROM fleet_messages WHERE message_id=?", (message_id,)
        ).fetchone()
    return _message_dict(row)


def claim_agent_messages(
    recipient_id: str, owner_id: str, *, project: str = "", limit: int = 8,
    now: float | None = None, principal_id: str = "",
    principal_secret: str = "",
) -> list[dict]:
    """Atomically claim queued messages and return explicit delivery receipts."""
    recipient = str(recipient_id or "").strip()
    owner = str(owner_id or "").strip()
    scoped_project = str(project or "")
    if not recipient or not owner:
        raise ValueError("recipient_id and owner_id are required")
    capped_limit = max(1, min(int(limit or 8), 32))
    current = float(now if now is not None else time.time())
    with _write_transaction() as conn:
        principal = ""
        if principal_id:
            principal = _authenticate_principal(
                conn, principal_id, principal_secret,
            )
        agent = conn.execute(
            "SELECT owner_id, principal_id, project FROM fleet_agents WHERE id=?",
            (recipient,),
        ).fetchone()
        if (
            agent is None
            or (
                str(agent["principal_id"] or "") != principal
                if principal else agent["owner_id"] != owner
            )
            or str(agent["project"] or "") != scoped_project
        ):
            raise PermissionError("recipient is outside the requested scope")
        conn.execute(
            "UPDATE fleet_messages SET status='expired' "
            "WHERE recipient_id=? AND status='queued' AND expires_ts<=?",
            (recipient, current),
        )
        inactive_placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        conn.execute(
            "UPDATE fleet_messages SET status='expired' "
            "WHERE recipient_id=? AND mode='steer' AND status='queued' "
            "AND EXISTS (SELECT 1 FROM fleet_agents WHERE id=? "
            "AND status NOT IN (%s))" % inactive_placeholders,
            (recipient, recipient, *sorted(ACTIVE_STATUSES)),
        )
        message_scope_column = "principal_id" if principal else "owner_id"
        ids = [
            row[0] for row in conn.execute(
                "SELECT message_id FROM fleet_messages WHERE recipient_id=? "
                "AND %s=? AND project_scope=? AND status='queued' "
                "ORDER BY queued_ts, message_id LIMIT ?" % message_scope_column,
                (
                    recipient, principal or owner, scoped_project, capped_limit,
                ),
            ).fetchall()
        ]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            "UPDATE fleet_messages SET status='delivered', delivered_ts=? "
            "WHERE status='queued' AND message_id IN (%s)" % placeholders,
            [current, *ids],
        )
        rows = conn.execute(
            "SELECT * FROM fleet_messages WHERE message_id IN (%s) "
            "ORDER BY queued_ts, message_id" % placeholders,
            ids,
        ).fetchall()
    return [_message_dict(row) for row in rows]


def list_agent_messages(
    agent_id: str, owner_id: str, *, project: str = "", limit: int = 50,
    principal_id: str = "", principal_secret: str = "",
) -> list[dict]:
    """List message receipts for one agent as sender or recipient."""
    agent = str(agent_id or "").strip()
    owner = str(owner_id or "").strip()
    scoped_project = str(project or "")
    if not agent or not owner:
        raise ValueError("agent_id and owner_id are required")
    capped_limit = max(1, min(int(limit or 50), 200))
    conn = _connect()
    try:
        principal = ""
        if principal_id:
            principal = _authenticate_principal(
                conn, principal_id, principal_secret,
            )
        scope_column = "principal_id" if principal else "owner_id"
        scope_value = principal or owner
        scoped = conn.execute(
            "SELECT owner_id, principal_id, project FROM fleet_agents WHERE id=?",
            (agent,),
        ).fetchone()
        if (
            scoped is None
            or str(scoped[scope_column] or "") != scope_value
            or str(scoped["project"] or "") != scoped_project
        ):
            raise PermissionError("agent is outside the requested scope")
        rows = conn.execute(
            "SELECT * FROM fleet_messages WHERE %s=? AND project_scope=? "
            "AND (sender_id=? OR recipient_id=?) "
            "ORDER BY queued_ts DESC, message_id DESC LIMIT ?" % scope_column,
            (scope_value, scoped_project, agent, agent, capped_limit),
        ).fetchall()
        return [_message_dict(row) for row in rows]
    finally:
        conn.close()


def add_event(agent_id: str, owner_id: str, stamp: str, message: str) -> None:
    with _write_transaction() as conn:
        agent = conn.execute(
            """
            SELECT master_task_digest, delegated_task_digest,
                   objective_ids_json, task_drift
            FROM fleet_agents WHERE id=?
            """,
            (agent_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO fleet_events(
                agent_id, owner_id, ts, stamp, message, master_task_digest,
                delegated_task_digest, objective_ids_json, task_drift
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id, owner_id, time.time(), _clamp_text(stamp, 40),
                _clamp_text(message, 2000),
                agent["master_task_digest"] if agent else "",
                agent["delegated_task_digest"] if agent else "",
                agent["objective_ids_json"] if agent else "[]",
                int(agent["task_drift"] if agent else 0),
            ),
        )


def snapshot(include_finished: bool = True, limit: int = 20) -> dict:
    reconcile = reconcile_stale_owners()
    conn = _connect()
    try:
        where = "" if include_finished else "WHERE status IN ('queued', 'running')"
        rows = conn.execute(
            "SELECT * FROM fleet_agents %s ORDER BY updated_ts DESC LIMIT ?" % where,
            (max(1, int(limit or 20)),),
        ).fetchall()
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS total_agents,
                SUM(CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END) AS active_agents,
                SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_agents,
                SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued_agents,
                -- Full-table count of agents currently inside a model call, so a
                -- caller gating on GPU contention sees the whole population and
                -- not the top-N by updated_ts that the paginated agents list
                -- carries.
                SUM(CASE WHEN status IN ('queued','running') AND in_model_call=1
                    THEN 1 ELSE 0 END) AS active_model_calls,
                SUM(CASE WHEN status='running' AND cancel_requested=1 THEN 1 ELSE 0 END) AS cancel_pending,
                SUM(CASE WHEN status='interrupted' THEN 1 ELSE 0 END) AS interrupted_agents,
                SUM(CASE WHEN status='task_drift' THEN 1 ELSE 0 END) AS task_drift_agents,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                COALESCE(SUM(CASE WHEN status IN ('queued','running')
                    THEN worker_slots ELSE 0 END), 0) AS worker_slots_total
            FROM fleet_agents
            """
        ).fetchone()
        latest = conn.execute(
            """
            SELECT id, task, project, output, updated_ts FROM fleet_agents
            WHERE role='master' AND status='done' AND output <> ''
            ORDER BY updated_ts DESC LIMIT 1
            """
        ).fetchone()
        events = conn.execute(
            """
            SELECT stamp AS ts, agent_id, message, master_task_digest,
                   delegated_task_digest, objective_ids_json, task_drift
            FROM fleet_events ORDER BY event_id DESC LIMIT 80
            """
        ).fetchall()
        return {
            "active_agents": int(totals["active_agents"] or 0),
            "running_agents": int(totals["running_agents"] or 0),
            "queued_agents": int(totals["queued_agents"] or 0),
            "active_model_calls": int(totals["active_model_calls"] or 0),
            "cancel_pending": int(totals["cancel_pending"] or 0),
            "interrupted_agents": int(totals["interrupted_agents"] or 0),
            "task_drift_agents": int(totals["task_drift_agents"] or 0),
            "total_agents": int(totals["total_agents"] or 0),
            "total_listed": len(rows),
            "agents": [_row_dict(row) for row in rows],
            "events": [_event_dict(row) for row in reversed(events)],
            "tokens_in": int(totals["tokens_in"] or 0),
            "tokens_out": int(totals["tokens_out"] or 0),
            "worker_slots_total": int(totals["worker_slots_total"] or 0),
            # Keep the scalar for API compatibility, but also return identity
            # and task context. A status view can otherwise place an older,
            # unrelated repository result directly beneath a live fleet and
            # make it look like evidence from that active run.
            "latest_master_result": latest["output"] if latest else "",
            "latest_master": dict(latest) if latest else {},
            "reconcile": reconcile,
            "database": database_path(),
        }
    finally:
        conn.close()


def prune(
    finished_retention: int = DEFAULT_FINISHED_RETENTION,
    event_retention: int = DEFAULT_EVENT_RETENTION,
    message_retention_seconds: int = DEFAULT_MESSAGE_RETENTION_SECONDS,
) -> dict:
    finished_retention = max(10, min(int(finished_retention), 10_000))
    event_retention = max(100, min(int(event_retention), 50_000))
    message_retention_seconds = max(
        3600, min(int(message_retention_seconds), 90 * 24 * 60 * 60)
    )
    now = time.time()
    with _write_transaction() as conn:
        # Interactive lanes retain their mailbox until explicit acknowledgement
        # and session retention. Legacy fleet TTL pruning must not delete them.
        lane_mailbox = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_lane_messages'"
        ).fetchone()
        retained_clause = (
            " AND message_id NOT IN (SELECT message_id FROM agent_lane_messages)"
            if lane_mailbox else ""
        )
        conn.execute(
            "UPDATE fleet_messages SET status='expired' "
            "WHERE status='queued' AND expires_ts<=?" + retained_clause,
            (now,),
        )
        before = conn.total_changes
        conn.execute(
            "DELETE FROM fleet_messages WHERE status IN ('delivered', 'expired') "
            "AND COALESCE(delivered_ts, queued_ts)<?" + retained_clause,
            (now - message_retention_seconds,),
        )
        deleted_messages = conn.total_changes - before
        before = conn.total_changes
        conn.execute(
            """
            DELETE FROM fleet_agents
            WHERE id IN (
                SELECT id FROM fleet_agents
                WHERE status NOT IN ('queued', 'running') AND role<>'agent_lane'
                ORDER BY updated_ts DESC LIMIT -1 OFFSET ?
            )
              AND id NOT IN (
                WITH RECURSIVE protected(id) AS (
                    SELECT sender_id FROM fleet_messages WHERE status='queued'
                    UNION
                    SELECT recipient_id FROM fleet_messages WHERE status='queued'
                    UNION
                    SELECT parent.parent_id
                    FROM fleet_agents AS parent
                    JOIN protected ON parent.id=protected.id
                    WHERE parent.parent_id<>''
                )
                SELECT id FROM protected
              )
            """,
            (finished_retention,),
        )
        deleted_agents = conn.total_changes - before
        before = conn.total_changes
        conn.execute(
            """
            DELETE FROM fleet_events
            WHERE event_id IN (
                SELECT event_id FROM fleet_events
                ORDER BY event_id DESC LIMIT -1 OFFSET ?
            )
            """,
            (event_retention,),
        )
        deleted_events = conn.total_changes - before
        conn.execute(
            """
            DELETE FROM fleet_owners
            WHERE closed_ts IS NOT NULL
              AND owner_id NOT IN (SELECT DISTINCT owner_id FROM fleet_agents)
            """
        )
    return {
        "agents": deleted_agents,
        "events": deleted_events,
        "messages": deleted_messages,
    }


def clear_all() -> None:
    with _write_transaction() as conn:
        conn.execute("DELETE FROM fleet_messages")
        conn.execute("DELETE FROM fleet_events")
        conn.execute("DELETE FROM fleet_agents")
        conn.execute("DELETE FROM fleet_owners")


def reset_schema_cache_for_tests() -> None:
    with _SCHEMA_LOCK:
        _INITIALIZED_PATHS.clear()
