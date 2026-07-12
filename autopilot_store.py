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

import sonder_paths
from process_liveness import pid_alive as _process_pid_alive


ACTIVE_STATUSES = ("planning", "running")
RESUMABLE_STATUSES = ("ready", "paused", "blocked", "interrupted")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
ALL_STATUSES = ACTIVE_STATUSES + RESUMABLE_STATUSES + TERMINAL_STATUSES
MAX_OBJECTIVE_CHARS = 32_000
MAX_PLAN_CHARS = 512_000
MAX_REPORT_CHARS = 128_000
MAX_ERROR_CHARS = 8_000
MAX_SUMMARY_CHARS = 4_000
MAX_EVENT_CHARS = 1_000
DEFAULT_LEASE_SECONDS = 3600

_SCHEMA_LOCK = threading.RLock()
_INITIALIZED_PATHS: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS autopilot_runs (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    project TEXT DEFAULT '',
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
"""

_RUN_COLUMN_MIGRATIONS = {
    "checkpoints": "INTEGER NOT NULL DEFAULT 0",
    "replans": "INTEGER NOT NULL DEFAULT 0",
    "max_replans": "INTEGER NOT NULL DEFAULT 2",
    "adaptive": "INTEGER NOT NULL DEFAULT 1",
}


def database_path() -> str:
    return sonder_paths.state_path("autopilot.db", "SONDER_AUTOPILOT_DB")


def _clamp_text(value, limit: int) -> str:
    return str(value or "")[:limit]


def _json_text(value, limit=MAX_PLAN_CHARS) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > limit:
        raise ValueError("autopilot JSON state exceeds %d characters" % limit)
    return text


def _ensure_schema(path: str) -> None:
    resolved = str(Path(path).expanduser().resolve())
    with _SCHEMA_LOCK:
        if resolved in _INITIALIZED_PATHS:
            return
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
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
        _INITIALIZED_PATHS.add(resolved)


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


def _pid_alive(pid: int) -> bool:
    return _process_pid_alive(pid)


def create_run(
    objective: str,
    *,
    project: str = "",
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
                id, objective, project, tier, policy, allow_web, status, phase,
                max_failures, max_tasks, max_replans, adaptive, created_ts, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?, 'ready', 'plan', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                objective,
                _clamp_text(project, 200),
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


def _resolve(conn, selector: str = ""):
    selector = str(selector or "").strip()
    if not selector or selector == "latest":
        return conn.execute(
            "SELECT * FROM autopilot_runs ORDER BY updated_ts DESC LIMIT 1"
        ).fetchone()
    exact = conn.execute(
        "SELECT * FROM autopilot_runs WHERE id=?", (selector,)
    ).fetchone()
    if exact is not None:
        return exact
    rows = conn.execute(
        "SELECT * FROM autopilot_runs WHERE id LIKE ? ORDER BY updated_ts DESC LIMIT 2",
        (selector + "%",),
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def get_run(selector: str = "") -> dict | None:
    reconcile_stale_runs()
    conn = _connect()
    try:
        return _row_dict(_resolve(conn, selector))
    finally:
        conn.close()


def list_runs(include_finished: bool = True, limit: int = 20) -> list[dict]:
    reconcile_stale_runs()
    limit = max(1, min(int(limit or 20), 100))
    conn = _connect()
    try:
        if include_finished:
            rows = conn.execute(
                "SELECT * FROM autopilot_runs ORDER BY updated_ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            marks = ",".join("?" for _ in TERMINAL_STATUSES)
            rows = conn.execute(
                "SELECT * FROM autopilot_runs WHERE status NOT IN (%s) "
                "ORDER BY updated_ts DESC LIMIT ?" % marks,
                (*TERMINAL_STATUSES, limit),
            ).fetchall()
        return [_row_dict(row) for row in rows]
    finally:
        conn.close()


def claim_run(
    selector: str,
    owner_id: str,
    *,
    owner_pid: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict | None:
    reconcile_stale_runs()
    now = time.time()
    lease = now + max(60, min(int(lease_seconds), 3600))
    with _write_transaction() as conn:
        found = _resolve(conn, selector)
        if found is None:
            return None
        row = dict(found)
        if row["status"] in TERMINAL_STATUSES or row.get("cancel_requested"):
            return None
        if row["status"] in ACTIVE_STATUSES and row.get("owner_id") != owner_id:
            return None
        next_status = "planning" if not json.loads(row.get("plan_json") or "[]") else "running"
        cursor = conn.execute(
            """
            UPDATE autopilot_runs
            SET status=?, phase=?, owner_id=?, owner_pid=?, owner_host=?,
                lease_until=?, pause_requested=0, updated_ts=?
            WHERE id=? AND status NOT IN ('completed', 'failed', 'cancelled')
                AND cancel_requested=0
            """,
            (
                next_status,
                "plan" if next_status == "planning" else "execute",
                owner_id,
                int(owner_pid),
                socket.gethostname(),
                lease,
                now,
                row["id"],
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


def request_pause(selector: str) -> dict | None:
    now = time.time()
    with _write_transaction() as conn:
        found = _resolve(conn, selector)
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


def request_cancel(selector: str) -> dict | None:
    now = time.time()
    with _write_transaction() as conn:
        found = _resolve(conn, selector)
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


def events(selector: str = "", limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit or 20), 100))
    conn = _connect()
    try:
        found = _resolve(conn, selector)
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


def snapshot(include_finished: bool = True, limit: int = 20) -> dict:
    rows = list_runs(include_finished=include_finished, limit=limit)
    conn = _connect()
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM autopilot_runs "
            "WHERE status IN ('planning', 'running')"
        ).fetchone()[0]
        resumable = conn.execute(
            "SELECT COUNT(*) FROM autopilot_runs "
            "WHERE status IN ('ready', 'paused', 'blocked', 'interrupted')"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM autopilot_runs").fetchone()[0]
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
        conn.execute("DELETE FROM autopilot_events")
        conn.execute("DELETE FROM autopilot_runs")


def reset_schema_cache_for_tests() -> None:
    with _SCHEMA_LOCK:
        _INITIALIZED_PATHS.clear()
