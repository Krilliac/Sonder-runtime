"""Process-safe persistence service for autonomous goal runs.

Ownership and lifecycle contract:

* This module exclusively owns ``autopilot.db`` and its schema.
* Public operations are safe from any local thread/process and use short SQLite
  transactions. Callers never mutate rows directly.
* Model calls and workspace tools are deliberately absent from this module.
* The module is non-hot-reloadable while a run is active; durable state remains
  valid when the controller/server process is replaced.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from sonder_runtime.adapters.process_liveness import pid_alive as _process_pid_alive
from sonder_runtime.domain.automation import state_machine as _sm
from sonder_runtime.platform import paths as _platform_paths


# SPEC-3 Phase 6: the canonical status classification lives in the domain
# state machine; these names remain the module's public surface.
ACTIVE_STATUSES = _sm.AUTOPILOT_ACTIVE
RESUMABLE_STATUSES = _sm.AUTOPILOT_RESUMABLE
TERMINAL_STATUSES = _sm.AUTOPILOT_TERMINAL
ALL_STATUSES = _sm.AUTOPILOT_ALL
MAX_OBJECTIVE_CHARS = 32_000
MAX_PLAN_CHARS = 512_000
MAX_REPORT_CHARS = 128_000
MAX_ERROR_CHARS = 8_000
MAX_SUMMARY_CHARS = 4_000
MAX_EVENT_CHARS = 1_000
MAX_STEERING_CHARS = 4_000
MAX_PENDING_STEERING = 5
STEERING_KINDS = ("guidance", "clarify")
DEFAULT_LEASE_SECONDS = 3600

_SCHEMA_LOCK = threading.RLock()
# Cache the database identity, not merely its pathname.  Backup/restore can
# atomically replace ``autopilot.db`` while this process stays alive; treating
# the replacement as initialized would then open an empty or older database
# without running the idempotent schema bootstrap.
_INITIALIZED_PATHS: dict[str, tuple[int, int]] = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS autopilot_runs (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    project TEXT DEFAULT '',
    request_owner TEXT DEFAULT '',
    tier TEXT NOT NULL,
    policy TEXT NOT NULL,
    allow_web INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    plan_json TEXT NOT NULL DEFAULT '[]',
    criteria_json TEXT NOT NULL DEFAULT '[]',
    plan_summary TEXT DEFAULT '',
    current_task INTEGER,
    cycles INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    checkpoints INTEGER NOT NULL DEFAULT 0,
    replans INTEGER NOT NULL DEFAULT 0,
    max_failures INTEGER NOT NULL DEFAULT 3,
    max_tasks INTEGER NOT NULL DEFAULT 12,
    max_replans INTEGER NOT NULL DEFAULT 2,
    adaptive INTEGER NOT NULL DEFAULT 1,
    owner_id TEXT DEFAULT '',
    owner_pid INTEGER DEFAULT 0,
    owner_host TEXT DEFAULT '',
    lease_until REAL,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    finished_ts REAL,
    summary TEXT DEFAULT '',
    final_report TEXT DEFAULT '',
    last_error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_autopilot_status
    ON autopilot_runs(status, updated_ts DESC);
CREATE TABLE IF NOT EXISTS autopilot_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES autopilot_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_autopilot_events_run
    ON autopilot_events(run_id, event_id DESC);
CREATE TABLE IF NOT EXISTS autopilot_steering (
    note_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    request_owner TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_ts REAL NOT NULL,
    consumed_ts REAL,
    consumed_by TEXT DEFAULT '',
    FOREIGN KEY(run_id) REFERENCES autopilot_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_autopilot_steering_run
    ON autopilot_steering(run_id, consumed_ts);
"""

_RUN_COLUMN_MIGRATIONS = {
    "request_owner": "TEXT DEFAULT ''",
    "checkpoints": "INTEGER NOT NULL DEFAULT 0",
    "replans": "INTEGER NOT NULL DEFAULT 0",
    "max_replans": "INTEGER NOT NULL DEFAULT 2",
    "adaptive": "INTEGER NOT NULL DEFAULT 1",
}


def database_path() -> str:
    return _platform_paths.state_path("autopilot.db", "SONDER_AUTOPILOT_DB")


def _clamp_text(value, limit: int) -> str:
    return str(value or "")[:limit]


def _json_text(value, limit=MAX_PLAN_CHARS) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > limit:
        raise ValueError("autopilot JSON state exceeds %d characters" % limit)
    return text


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
        conn = sqlite3.connect(resolved, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            existing = {
                row[1] for row in conn.execute("PRAGMA table_info(autopilot_runs)")
            }
            for name, declaration in _RUN_COLUMN_MIGRATIONS.items():
                if name not in existing:
                    conn.execute(
                        "ALTER TABLE autopilot_runs ADD COLUMN %s %s"
                        % (name, declaration)
                    )
            conn.commit()
        finally:
            conn.close()
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(resolved, 0o600)
        identity = _database_identity(resolved_path)
        if identity is None:  # pragma: no cover - SQLite just committed it
            raise RuntimeError("autopilot database disappeared during schema setup")
        _INITIALIZED_PATHS[resolved] = identity


def _connect() -> sqlite3.Connection:
    path = database_path()
    _ensure_schema(path)
    conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
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
    for source, target in (("plan_json", "plan"), ("criteria_json", "criteria")):
        try:
            parsed = json.loads(data.pop(source, "[]") or "[]")
        except (TypeError, ValueError):
            parsed = []
        data[target] = parsed if isinstance(parsed, list) else []
    data["allow_web"] = bool(data.get("allow_web"))
    data["adaptive"] = bool(data.get("adaptive"))
    data["pause_requested"] = bool(data.get("pause_requested"))
    data["cancel_requested"] = bool(data.get("cancel_requested"))
    return data


def _event(conn, run_id: str, kind: str, message: str, now=None) -> None:
    conn.execute(
        "INSERT INTO autopilot_events(run_id, ts, kind, message) VALUES (?, ?, ?, ?)",
        (
            run_id,
            float(now or time.time()),
            _clamp_text(kind or "event", 40),
            _clamp_text(message, MAX_EVENT_CHARS),
        ),
    )


def _completion_evidence_reason(row: sqlite3.Row) -> str:
    """Return why a durable row cannot be terminally marked completed.

    The controller performs richer, in-memory completion checks, but this
    repository is also the persistence boundary exposed through the application
    port.  A direct caller must not be able to turn model prose (or an empty
    ledger) into a durable completed run by bypassing the controller.  Read the
    row inside the same write transaction that will publish the terminal event.
    """
    try:
        plan = json.loads(row["plan_json"] or "[]")
        criteria = json.loads(row["criteria_json"] or "[]")
    except (TypeError, ValueError):
        return "persisted completion ledger is not valid JSON"
    if not isinstance(plan, list) or not plan:
        return "no persisted task ledger"
    if not isinstance(criteria, list) or not any(str(item).strip() for item in criteria):
        return "no persisted success criteria"

    validation_seen = False
    for task in plan:
        if not isinstance(task, dict):
            return "persisted task ledger contains a malformed task"
        status = str(task.get("status") or "")
        if status not in ("passed", "superseded"):
            return "task %s is not durably passed" % (task.get("id") or "?")
        if status == "superseded":
            continue
        receipt = task.get("host_receipt")
        if not isinstance(receipt, dict) or not receipt.get("tools"):
            return "task %s has no durable host receipt" % (task.get("id") or "?")
        kind = str(task.get("kind") or "")
        if kind == "implement" and receipt.get("mutation_observed") is not True:
            return "implementation task %s lacks a durable mutation receipt" % (
                task.get("id") or "?"
            )
        if kind == "validate":
            if (
                receipt.get("validation_attempted") is not True
                or receipt.get("validation_passed") is not True
            ):
                return "validation task %s lacks a durable passing receipt" % (
                    task.get("id") or "?"
                )
            validation_seen = True
    if not validation_seen:
        return "no durably passing validation receipt"
    return ""


def _pid_alive(pid: int) -> bool:
    return _process_pid_alive(pid)


def create_run(
    objective: str,
    *,
    project: str = "",
    request_owner: str = "",
    tier: str = "code",
    policy: str = "workspace",
    allow_web: bool = True,
    max_failures: int = 3,
    max_tasks: int = 12,
    max_replans: int = 2,
    adaptive: bool = True,
) -> dict:
    objective = _clamp_text(objective.strip(), MAX_OBJECTIVE_CHARS)
    if not objective:
        raise ValueError("autopilot objective is required")
    run_id = "auto-%s" % uuid.uuid4().hex[:12]
    now = time.time()
    with _write_transaction() as conn:
        conn.execute(
            """
            INSERT INTO autopilot_runs(
                id, objective, project, request_owner, tier, policy, allow_web, status, phase,
                max_failures, max_tasks, max_replans, adaptive, created_ts, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', 'plan', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                objective,
                _clamp_text(project, 200),
                _clamp_text(request_owner, 128),
                _clamp_text(tier, 40),
                _clamp_text(policy, 40),
                int(bool(allow_web)),
                max(1, min(int(max_failures), 10)),
                max(3, min(int(max_tasks), 24)),
                max(0, min(int(max_replans), 6)),
                int(bool(adaptive)),
                now,
                now,
            ),
        )
        _event(conn, run_id, "created", "autopilot goal created", now)
        row = conn.execute(
            "SELECT * FROM autopilot_runs WHERE id=?", (run_id,)
        ).fetchone()
    return _row_dict(row)


def _resolve(conn, selector: str = "", request_owner: str | None = None):
    selector = str(selector or "").strip()
    scope_sql = " AND request_owner=?" if request_owner is not None else ""
    scope_args = (request_owner,) if request_owner is not None else ()
    if not selector or selector == "latest":
        return conn.execute(
            "SELECT * FROM autopilot_runs WHERE 1=1%s ORDER BY updated_ts DESC LIMIT 1" % scope_sql,
            scope_args,
        ).fetchone()
    exact = conn.execute(
        "SELECT * FROM autopilot_runs WHERE id=?%s" % scope_sql,
        (selector, *scope_args),
    ).fetchone()
    if exact is not None:
        return exact
    # Prefix matching treats the selector as literal text, exactly like
    # fleet_store.get_agent: an unescaped LIKE would let "auto_" or "%" match
    # unrelated runs and resolve an unintended target when only one exists.
    escaped = selector.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT * FROM autopilot_runs WHERE id LIKE ? ESCAPE '\\'%s "
        "ORDER BY updated_ts DESC LIMIT 2" % scope_sql,
        (escaped + "%", *scope_args),
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def get_run(selector: str = "", request_owner: str | None = None) -> dict | None:
    reconcile_stale_runs()
    conn = _connect()
    try:
        return _row_dict(_resolve(conn, selector, request_owner))
    finally:
        conn.close()


def list_runs(include_finished: bool = True, limit: int = 20, request_owner: str | None = None) -> list[dict]:
    reconcile_stale_runs()
    limit = max(1, min(int(limit or 20), 100))
    conn = _connect()
    try:
        scope_sql = " AND request_owner=?" if request_owner is not None else ""
        scope_args = (request_owner,) if request_owner is not None else ()
        if include_finished:
            rows = conn.execute(
                "SELECT * FROM autopilot_runs WHERE 1=1%s ORDER BY updated_ts DESC LIMIT ?" % scope_sql,
                (*scope_args, limit),
            ).fetchall()
        else:
            marks = ",".join("?" for _ in TERMINAL_STATUSES)
            rows = conn.execute(
                "SELECT * FROM autopilot_runs WHERE status NOT IN (%s)%s "
                "ORDER BY updated_ts DESC LIMIT ?" % (marks, scope_sql),
                (*TERMINAL_STATUSES, *scope_args, limit),
            ).fetchall()
        return [_row_dict(row) for row in rows]
    finally:
        conn.close()


def claim_run(
    selector: str,
    owner_id: str,
    *,
    owner_pid: int,
    request_owner: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict | None:
    reconcile_stale_runs()
    now = time.time()
    lease = now + max(60, min(int(lease_seconds), 3600))
    with _write_transaction() as conn:
        found = _resolve(conn, selector, request_owner)
        if found is None:
            return None
        row = dict(found)
        if row["status"] in TERMINAL_STATUSES or row.get("cancel_requested"):
            return None
        if row["status"] in ACTIVE_STATUSES and row.get("owner_id") != owner_id:
            return None
        next_status = "planning" if not json.loads(row.get("plan_json") or "[]") else "running"
        # State machine is the single source of truth for valid transitions.
        current_status = row["status"]
        if current_status != next_status and not _sm.autopilot_can_transition(
            current_status, next_status
        ):
            raise ValueError(
                "autopilot state machine does not allow %s -> %s"
                % (current_status, next_status)
            )
        cursor = conn.execute(
            """
            UPDATE autopilot_runs
            SET status=?, phase=?, owner_id=?, owner_pid=?, owner_host=?,
                lease_until=?, pause_requested=0, updated_ts=?
            WHERE id=? AND status NOT IN ('completed', 'failed', 'cancelled')
                AND cancel_requested=0
                %s
            """ % ("AND request_owner=?" if request_owner is not None else ""),
            (
                next_status,
                "plan" if next_status == "planning" else "execute",
                owner_id,
                int(owner_pid),
                socket.gethostname(),
                lease,
                now,
                row["id"],
                *((request_owner,) if request_owner is not None else ()),
            ),
        )
        if cursor.rowcount <= 0:
            return None
        _event(conn, row["id"], "claimed", "run claimed by local controller", now)
        stored = conn.execute(
            "SELECT * FROM autopilot_runs WHERE id=?", (row["id"],)
        ).fetchone()
    return _row_dict(stored)


def save_progress(
    run_id: str,
    owner_id: str,
    *,
    plan=None,
    criteria=None,
    plan_summary: str | None = None,
    status: str | None = None,
    phase: str | None = None,
    current_task: int | None = None,
    cycles_delta: int = 0,
    failures_delta: int = 0,
    checkpoints_delta: int = 0,
    replans_delta: int = 0,
    summary: str | None = None,
    last_error: str | None = None,
    event_kind: str = "progress",
    event_message: str = "autopilot progress saved",
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict | None:
    now = time.time()
    assignments = [
        "cycles=cycles+?", "failures=failures+?", "checkpoints=checkpoints+?",
        "replans=replans+?", "lease_until=?", "updated_ts=?",
    ]
    values: list[object] = [
        int(cycles_delta), int(failures_delta),
        int(checkpoints_delta), int(replans_delta),
        now + max(60, min(int(lease_seconds), 3600)), now,
    ]
    if plan is not None:
        assignments.append("plan_json=?")
        values.append(_json_text(plan))
    if criteria is not None:
        assignments.append("criteria_json=?")
        values.append(_json_text(criteria, 64_000))
    if plan_summary is not None:
        assignments.append("plan_summary=?")
        values.append(_clamp_text(plan_summary, MAX_SUMMARY_CHARS))
    if status is not None:
        if status not in ALL_STATUSES:
            raise ValueError("invalid autopilot status: %s" % status)
        assignments.append("status=?")
        values.append(status)
    if phase is not None:
        assignments.append("phase=?")
        values.append(_clamp_text(phase, 40))
    if current_task is not None:
        assignments.append("current_task=?")
        values.append(None if int(current_task) < 0 else int(current_task))
    if summary is not None:
        assignments.append("summary=?")
        values.append(_clamp_text(summary, MAX_SUMMARY_CHARS))
    if last_error is not None:
        assignments.append("last_error=?")
        values.append(_clamp_text(last_error, MAX_ERROR_CHARS))
    values.extend([run_id, owner_id])
    with _write_transaction() as conn:
        # State machine is the single source of truth for valid transitions.
        if status is not None:
            existing = conn.execute(
                "SELECT status FROM autopilot_runs WHERE id=? AND owner_id=? "
                "AND status IN ('planning', 'running') AND cancel_requested=0",
                (run_id, owner_id),
            ).fetchone()
            if existing is not None and existing["status"] != status:
                if not _sm.autopilot_can_transition(existing["status"], status):
                    raise ValueError(
                        "autopilot state machine does not allow %s -> %s"
                        % (existing["status"], status)
                    )
        cursor = conn.execute(
            "UPDATE autopilot_runs SET %s WHERE id=? AND owner_id=? "
            "AND status IN ('planning', 'running') AND cancel_requested=0"
            % ", ".join(assignments),
            values,
        )
        if cursor.rowcount <= 0:
            return None
        _event(conn, run_id, event_kind, event_message, now)
        row = conn.execute(
            "SELECT * FROM autopilot_runs WHERE id=?", (run_id,)
        ).fetchone()
    return _row_dict(row)


def request_pause(selector: str, request_owner: str | None = None) -> dict | None:
    now = time.time()
    with _write_transaction() as conn:
        found = _resolve(conn, selector, request_owner)
        if found is None:
            return None
        row = dict(found)
        if row["status"] in TERMINAL_STATUSES:
            return _row_dict(found)
        if row["status"] in ACTIVE_STATUSES:
            conn.execute(
                "UPDATE autopilot_runs SET pause_requested=1, updated_ts=? WHERE id=?",
                (now, row["id"]),
            )
            message = "pause requested; active task will finish first"
        else:
            # State machine is the single source of truth for valid
            # transitions.
            if row["status"] != "paused" and not _sm.autopilot_can_transition(
                row["status"], "paused"
            ):
                return _row_dict(found)
            conn.execute(
                """
                UPDATE autopilot_runs
                SET status='paused', phase='paused', pause_requested=0,
                    owner_id='', owner_pid=0, owner_host='', lease_until=NULL,
                    updated_ts=? WHERE id=?
                """,
                (now, row["id"]),
            )
            message = "run paused"
        _event(conn, row["id"], "pause", message, now)
        stored = conn.execute(
            "SELECT * FROM autopilot_runs WHERE id=?", (row["id"],)
        ).fetchone()
    return _row_dict(stored)


def request_cancel(selector: str, request_owner: str | None = None) -> dict | None:
    now = time.time()
    with _write_transaction() as conn:
        found = _resolve(conn, selector, request_owner)
        if found is None:
            return None
        row = dict(found)
        if row["status"] in TERMINAL_STATUSES:
            return _row_dict(found)
        if row["status"] in ACTIVE_STATUSES:
            conn.execute(
                "UPDATE autopilot_runs SET cancel_requested=1, updated_ts=? WHERE id=?",
                (now, row["id"]),
            )
            message = "cancellation requested; active task result will be discarded"
        else:
            conn.execute(
                """
                UPDATE autopilot_runs
                SET status='cancelled', phase='cancelled', cancel_requested=1,
                    owner_id='', owner_pid=0, owner_host='', lease_until=NULL,
                    finished_ts=?, updated_ts=? WHERE id=?
                """,
                (now, now, row["id"]),
            )
            message = "run cancelled"
        _event(conn, row["id"], "cancel", message, now)
        stored = conn.execute(
            "SELECT * FROM autopilot_runs WHERE id=?", (row["id"],)
        ).fetchone()
    return _row_dict(stored)


def control_flags(run_id: str, owner_id: str) -> dict:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT status, pause_requested, cancel_requested, owner_id "
            "FROM autopilot_runs WHERE id=?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None or row["owner_id"] != owner_id:
        return {"lost": True, "pause": False, "cancel": True}
    return {
        "lost": False,
        "pause": bool(row["pause_requested"]),
        "cancel": bool(row["cancel_requested"]),
        "status": row["status"],
    }


def attach_steering(
    selector: str,
    message: str,
    *,
    kind: str = "guidance",
    request_owner: str | None,
) -> dict | None:
    """Attach a bounded owner note to a non-terminal run.

    Strictly owner-scoped and fail-closed: the caller must present a non-empty
    ``request_owner`` that equals the run's stored owner, so legacy/unowned
    rows (empty ``request_owner``) can never be steered and a foreign account
    cannot even learn that the run exists.  ``clarify`` additionally requests
    a cooperative pause on an active run through the existing pause fence; the
    note itself is delivered only when the live lease owner asks for it.
    """
    if kind not in STEERING_KINDS:
        raise ValueError("invalid steering kind: %s" % kind)
    message = _clamp_text(str(message or "").strip(), MAX_STEERING_CHARS)
    if not message:
        raise ValueError("steering message is required")
    owner = str(request_owner or "")
    if not owner:
        return None
    now = time.time()
    with _write_transaction() as conn:
        found = _resolve(conn, selector, owner)
        if found is None:
            return None
        row = dict(found)
        if not row.get("request_owner") or row["request_owner"] != owner:
            return None
        if row["status"] in TERMINAL_STATUSES or row.get("cancel_requested"):
            return None
        pending = conn.execute(
            "SELECT COUNT(*) FROM autopilot_steering "
            "WHERE run_id=? AND consumed_ts IS NULL",
            (row["id"],),
        ).fetchone()[0]
        if int(pending) >= MAX_PENDING_STEERING:
            return None
        cursor = conn.execute(
            "INSERT INTO autopilot_steering(run_id, request_owner, kind, message, created_ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["id"], owner, kind, message, now),
        )
        note_id = cursor.lastrowid
        if kind == "clarify" and row["status"] in ACTIVE_STATUSES:
            conn.execute(
                "UPDATE autopilot_runs SET pause_requested=1, updated_ts=? WHERE id=?",
                (now, row["id"]),
            )
        _event(conn, row["id"], "steer", "%s note attached by run owner" % kind, now)
    return {
        "note_id": int(note_id),
        "run_id": row["id"],
        "kind": kind,
        "message": message,
        "created_ts": now,
    }


def pending_steering(run_id: str, owner_id: str) -> list[dict]:
    """Return unconsumed owner notes, but only to the live lease owner.

    A worker that lost its lease (or a run that is paused, cancelled, or
    terminal) reads nothing: steering is delivered exclusively inside an
    active, owned execution so a stale controller can never act on it.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT owner_id, status, cancel_requested FROM autopilot_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if (
            row is None
            or not owner_id
            or row["owner_id"] != owner_id
            or row["status"] not in ("planning", "running")
            or row["cancel_requested"]
        ):
            return []
        rows = conn.execute(
            "SELECT note_id, run_id, kind, message, created_ts FROM autopilot_steering "
            "WHERE run_id=? AND consumed_ts IS NULL ORDER BY note_id LIMIT ?",
            (run_id, MAX_PENDING_STEERING),
        ).fetchall()
        return [dict(item) for item in rows]
    finally:
        conn.close()


def consume_steering(run_id: str, owner_id: str, note_ids) -> int:
    """Durably mark notes as delivered; one-shot, live-owner only.

    Consumption never replays: once marked, a note is not redelivered after a
    pause, crash, or restart, matching the run ledger's no-auto-replay rule.
    """
    ids = [int(value) for value in (note_ids or [])]
    if not ids or not owner_id:
        return 0
    now = time.time()
    with _write_transaction() as conn:
        row = conn.execute(
            "SELECT owner_id, status, cancel_requested FROM autopilot_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if (
            row is None
            or row["owner_id"] != owner_id
            or row["status"] not in ("planning", "running")
            or row["cancel_requested"]
        ):
            return 0
        marks = ",".join("?" for _ in ids)
        cursor = conn.execute(
            "UPDATE autopilot_steering SET consumed_ts=?, consumed_by=? "
            "WHERE run_id=? AND consumed_ts IS NULL AND note_id IN (%s)" % marks,
            (now, owner_id, run_id, *ids),
        )
        consumed = int(cursor.rowcount)
        if consumed > 0:
            _event(
                conn, run_id, "steer_consumed",
                "%d steering note(s) delivered to the worker" % consumed, now,
            )
    return consumed


def heartbeat(
    run_id: str,
    owner_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Renew an active controller lease without changing visible progress."""
    now = time.time()
    lease = now + max(60, min(int(lease_seconds), 21_600))
    with _write_transaction() as conn:
        cursor = conn.execute(
            "UPDATE autopilot_runs SET lease_until=? "
            "WHERE id=? AND owner_id=? AND status IN ('planning', 'running')",
            (lease, run_id, owner_id),
        )
        return cursor.rowcount > 0


def finish_run(
    run_id: str,
    owner_id: str,
    status: str,
    *,
    summary: str = "",
    final_report: str = "",
    last_error: str = "",
) -> dict | None:
    if status not in ("paused", "blocked", *TERMINAL_STATUSES):
        raise ValueError("invalid autopilot terminal/release status: %s" % status)
    now = time.time()
    finished = now if status in TERMINAL_STATUSES else None
    with _write_transaction() as conn:
        if status == "completed":
            existing = conn.execute(
                "SELECT * FROM autopilot_runs WHERE id=? AND owner_id=? "
                "AND status IN ('planning', 'running')",
                (run_id, owner_id),
            ).fetchone()
            if existing is None:
                return None
            evidence_reason = _completion_evidence_reason(existing)
            if evidence_reason:
                _event(
                    conn, run_id, "completion_refused",
                    "completion refused: %s" % evidence_reason, now,
                )
                return None
        cursor = conn.execute(
            """
            UPDATE autopilot_runs
            SET status=?, phase=?, summary=?, final_report=?, last_error=?,
                pause_requested=0, owner_id='', owner_pid=0, owner_host='',
                lease_until=NULL, current_task=NULL, finished_ts=?, updated_ts=?
            WHERE id=? AND owner_id=? AND status IN ('planning', 'running')
            """,
            (
                status,
                status,
                _clamp_text(summary, MAX_SUMMARY_CHARS),
                _clamp_text(final_report, MAX_REPORT_CHARS),
                _clamp_text(last_error, MAX_ERROR_CHARS),
                finished,
                now,
                run_id,
                owner_id,
            ),
        )
        if cursor.rowcount <= 0:
            return None
        _event(conn, run_id, status, summary or status, now)
        row = conn.execute(
            "SELECT * FROM autopilot_runs WHERE id=?", (run_id,)
        ).fetchone()
    return _row_dict(row)


def reconcile_stale_runs(now: float | None = None) -> int:
    current = float(now or time.time())
    host = socket.gethostname()
    changed = 0
    with _write_transaction() as conn:
        rows = conn.execute(
            "SELECT id, owner_pid, owner_host, lease_until FROM autopilot_runs "
            "WHERE status IN ('planning', 'running')"
        ).fetchall()
        for row in rows:
            expired = row["lease_until"] is None or float(row["lease_until"]) < current
            dead_local = row["owner_host"] == host and not _pid_alive(row["owner_pid"])
            if not (expired or dead_local):
                continue
            conn.execute(
                """
                UPDATE autopilot_runs
                SET status='interrupted', phase='interrupted', owner_id='',
                    owner_pid=0, owner_host='', lease_until=NULL,
                    current_task=NULL, updated_ts=?
                WHERE id=? AND status IN ('planning', 'running')
                """,
                (current, row["id"]),
            )
            _event(
                conn, row["id"], "interrupted",
                "controller process or lease ended; explicit resume is required",
                current,
            )
            changed += 1
    return changed


def events(selector: str = "", limit: int = 20, request_owner: str | None = None) -> list[dict]:
    limit = max(1, min(int(limit or 20), 100))
    conn = _connect()
    try:
        found = _resolve(conn, selector, request_owner)
        if found is None:
            return []
        rows = conn.execute(
            "SELECT event_id, run_id, ts, kind, message FROM autopilot_events "
            "WHERE run_id=? ORDER BY event_id DESC LIMIT ?",
            (found["id"], limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]
    finally:
        conn.close()


def snapshot(include_finished: bool = True, limit: int = 20, request_owner: str | None = None) -> dict:
    rows = list_runs(include_finished=include_finished, limit=limit, request_owner=request_owner)
    conn = _connect()
    try:
        owner_sql = " AND request_owner=?" if request_owner is not None else ""
        owner_args = (request_owner,) if request_owner is not None else ()
        active = conn.execute(
            "SELECT COUNT(*) FROM autopilot_runs "
            "WHERE status IN ('planning', 'running')%s" % owner_sql,
            owner_args,
        ).fetchone()[0]
        resumable = conn.execute(
            "SELECT COUNT(*) FROM autopilot_runs "
            "WHERE status IN ('ready', 'paused', 'blocked', 'interrupted')%s" % owner_sql,
            owner_args,
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM autopilot_runs WHERE 1=1%s" % owner_sql, owner_args).fetchone()[0]
    finally:
        conn.close()
    latest = rows[0] if rows else None
    return {
        "active_runs": int(active),
        "resumable_runs": int(resumable),
        "total_runs": int(total),
        "total_listed": len(rows),
        "runs": rows,
        "latest": latest,
        "database": database_path(),
    }


def clear_all() -> None:
    with _write_transaction() as conn:
        conn.execute("DELETE FROM autopilot_steering")
        conn.execute("DELETE FROM autopilot_events")
        conn.execute("DELETE FROM autopilot_runs")


def reset_schema_cache_for_tests() -> None:
    with _SCHEMA_LOCK:
        _INITIALIZED_PATHS.clear()
