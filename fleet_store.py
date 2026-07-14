"""Process-shared durable ledger for master/subagent orchestration.

Ownership and concurrency contract:

* This module is a service, not per-agent state. It owns the SQLite schema and
  short transactions; ``master_orchestrator`` owns execution threads/model calls.
* Every public operation is safe from any thread or local Sonder process.
* Queued -> running, finish, cancellation, and stale-owner reconciliation are
  conditional ``BEGIN IMMEDIATE`` transactions. Callers never write rows directly.
* The module is intentionally not hot-reloaded. Its database survives process
  replacement; callers may reload while continuing to use this stable API.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path

import sonder_paths


ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("done", "failed", "cancelled", "interrupted", "retried")
DEFAULT_STALE_SECONDS = 60
DEFAULT_STALE_GRACE_SECONDS = 10
DEFAULT_FINISHED_RETENTION = 500
DEFAULT_EVENT_RETENTION = 2000
MAX_TASK_CHARS = 32_000
MAX_OUTPUT_CHARS = 128_000
MAX_ERROR_CHARS = 8_000
MAX_SUMMARY_CHARS = 2_000
MAX_ACTIVITY_CHARS = 500

_SCHEMA_LOCK = threading.RLock()
_INITIALIZED_PATHS: set[str] = set()

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
CREATE TABLE IF NOT EXISTS fleet_agents (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
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
    retried_by TEXT DEFAULT ''
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
    FOREIGN KEY(agent_id) REFERENCES fleet_agents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fleet_events_agent
    ON fleet_events(agent_id, event_id DESC);
"""


def database_path() -> str:
    return sonder_paths.state_path("fleet.db", "SONDER_FLEET_DB")


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


def _ensure_schema(path: str) -> None:
    resolved = str(Path(path).expanduser().resolve())
    with _SCHEMA_LOCK:
        if resolved in _INITIALIZED_PATHS:
            return
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        last_error = None
        for attempt in range(4):
            conn = None
            try:
                conn = sqlite3.connect(resolved, timeout=5)
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(fleet_agents)"
                    ).fetchall()
                }
                if "retry_of" not in columns:
                    try:
                        conn.execute(
                            "ALTER TABLE fleet_agents ADD COLUMN retry_of TEXT DEFAULT ''"
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                if "retried_by" not in columns:
                    try:
                        conn.execute(
                            "ALTER TABLE fleet_agents ADD COLUMN retried_by TEXT DEFAULT ''"
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                if "project" not in columns:
                    try:
                        conn.execute(
                            "ALTER TABLE fleet_agents ADD COLUMN project TEXT DEFAULT ''"
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                conn.commit()
                if os.name != "nt":
                    with contextlib.suppress(OSError):
                        os.chmod(resolved, 0o600)
                _INITIALIZED_PATHS.add(resolved)
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
    try:
        data["files"] = json.loads(data.pop("files_json", "[]") or "[]")
    except (TypeError, ValueError):
        data["files"] = []
    data["cancel_requested"] = bool(data.get("cancel_requested"))
    data["in_model_call"] = bool(data.get("in_model_call"))
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


def create_agent(row: dict, owner_id: str, owner_pid: int) -> dict:
    now = float(row.get("updated_ts") or time.time())
    status = str(row.get("status") or "queued")
    activity = _clamp_text(row.get("activity") or status, MAX_ACTIVITY_CHARS)
    summary = _clamp_text(row.get("summary"), MAX_SUMMARY_CHARS)
    cancel_requested = bool(row.get("cancel_requested"))
    finished_ts = row.get("finished_ts")
    with _write_transaction() as conn:
        parent_id = str(row.get("parent_id") or "")
        if parent_id:
            parent = conn.execute(
                "SELECT status, cancel_requested FROM fleet_agents WHERE id=?",
                (parent_id,),
            ).fetchone()
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
                id, owner_id, owner_pid, role, parent_id, task, status,
                activity, started_ts, updated_ts, finished_ts, tool_calls,
                tokens_in, tokens_out, files_json, summary, output, error,
                cancel_requested, in_model_call, requested_agents,
                worker_slots, mode, tier, project, retry_of, retried_by
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                row["id"], owner_id, int(owner_pid), row.get("role", "agent"),
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


def finish_agent(
    agent_id: str, owner_id: str, *, output: str = "", error: str = "",
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
        elif current["status"] in ("done", "failed"):
            marker = current.get("output") or (
                "ERROR: %s" % current.get("error") if current.get("error") else ""
            )
            return current, marker
        else:
            status = "failed" if error else "done"
            activity = "failed: %s" % _clamp_text(error, 160) if error else "finished"
            summary = _clamp_text(output or error, MAX_SUMMARY_CHARS)
            final_output = _clamp_text(output, MAX_OUTPUT_CHARS)
            final_error = _clamp_text(error, MAX_ERROR_CHARS)
            marker = output
            tokens_out = max(0, (len(output or "") + 3) // 4)
        conn.execute(
            """
            UPDATE fleet_agents
            SET status=?, activity=?, summary=?, output=?, error=?,
                tokens_out=?, in_model_call=0, finished_ts=?, updated_ts=?
            WHERE id=? AND owner_id=?
            """,
            (
                status, activity, summary, final_output, final_error, tokens_out,
                now, now, agent_id, owner_id,
            ),
        )
        stored = conn.execute(
            "SELECT * FROM fleet_agents WHERE id=?", (agent_id,)
        ).fetchone()
        stored_dict = _row_dict(stored)
        if (
            stored_dict
            and stored_dict.get("role") == "master"
            and stored_dict.get("status") == "done"
            and stored_dict.get("retry_of")
        ):
            conn.execute(
                """
                UPDATE fleet_agents
                SET status='retried', retried_by=?,
                    activity=?, updated_ts=?
                WHERE id=? AND status IN ('interrupted', 'failed', 'cancelled')
                """,
                (
                    agent_id, "retried successfully as %s" % agent_id,
                    now, stored_dict["retry_of"],
                ),
            )
    return _row_dict(stored), marker


def cancel_agents(selector: str) -> dict:
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


def add_event(agent_id: str, owner_id: str, stamp: str, message: str) -> None:
    with _write_transaction() as conn:
        conn.execute(
            """
            INSERT INTO fleet_events(agent_id, owner_id, ts, stamp, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                agent_id, owner_id, time.time(), _clamp_text(stamp, 40),
                _clamp_text(message, 2000),
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
                SUM(CASE WHEN status='running' AND cancel_requested=1 THEN 1 ELSE 0 END) AS cancel_pending,
                SUM(CASE WHEN status='interrupted' THEN 1 ELSE 0 END) AS interrupted_agents,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out
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
            SELECT stamp AS ts, agent_id, message
            FROM fleet_events ORDER BY event_id DESC LIMIT 80
            """
        ).fetchall()
        return {
            "active_agents": int(totals["active_agents"] or 0),
            "cancel_pending": int(totals["cancel_pending"] or 0),
            "interrupted_agents": int(totals["interrupted_agents"] or 0),
            "total_agents": int(totals["total_agents"] or 0),
            "total_listed": len(rows),
            "agents": [_row_dict(row) for row in rows],
            "events": [dict(row) for row in reversed(events)],
            "tokens_in": int(totals["tokens_in"] or 0),
            "tokens_out": int(totals["tokens_out"] or 0),
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
) -> dict:
    finished_retention = max(10, min(int(finished_retention), 10_000))
    event_retention = max(100, min(int(event_retention), 50_000))
    with _write_transaction() as conn:
        before = conn.total_changes
        conn.execute(
            """
            DELETE FROM fleet_agents
            WHERE id IN (
                SELECT id FROM fleet_agents
                WHERE status NOT IN ('queued', 'running')
                ORDER BY updated_ts DESC LIMIT -1 OFFSET ?
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
    return {"agents": deleted_agents, "events": deleted_events}


def clear_all() -> None:
    with _write_transaction() as conn:
        conn.execute("DELETE FROM fleet_events")
        conn.execute("DELETE FROM fleet_agents")
        conn.execute("DELETE FROM fleet_owners")


def reset_schema_cache_for_tests() -> None:
    with _SCHEMA_LOCK:
        _INITIALIZED_PATHS.clear()
