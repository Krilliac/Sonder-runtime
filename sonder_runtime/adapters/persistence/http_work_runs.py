"""Durable records of routed HTTP work runs: status, cancel flag, and answer.

A routed workbench/fleet/autopilot turn can outlive the HTTP request that
started it (client timeout, proxy timeout, or the served wait budget).  This
store is what lets the caller come back for the answer, ask for the run to
stop, and learn after a restart that a run was interrupted rather than
silently lost.

It retains only an opaque run id, an opaque owner scope (a digest of the
principal, never an account name), timestamps, a closed status, the cancel
flag, and the bounded answer text the caller would otherwise have received in
the chat reply.  The database is created ``0600``.

Bounds: answers are truncated to ``MAX_OUTPUT_CHARS``; finished rows expire
after ``retention_seconds()`` and the table keeps at most ``row_limit()`` rows
(oldest finished first).  ``running`` rows are never pruned; a row left
``running`` by an earlier process is reconciled to ``interrupted``.

Terminal statuses beyond the lane's own return: ``refused`` (the host or lane
declined), ``failed`` (an error or an unmet evidence requirement), ``busy`` (no
capacity; retryable) and ``delegated`` (the work started a fleet or Autopilot
run that continues under its own id, listed in the narration links).
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect
from sonder_runtime.domain.security import redaction
from sonder_runtime.platform import paths as sonder_paths

STATUSES = (
    "running", "returned", "unknown", "refused", "cancelled",
    "budget_exceeded", "interrupted", "failed", "busy", "delegated",
)
TERMINAL_STATUSES = frozenset(STATUSES) - {"running"}
MAX_OUTPUT_CHARS = 256 * 1024

_LOCK = threading.RLock()
_INITIALIZED: set[str] = set()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_work_runs (
    run_id TEXT PRIMARY KEY,
    owner_scope TEXT NOT NULL,
    process_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (%s)),
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    deadline_ts REAL NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    output TEXT NOT NULL DEFAULT '',
    output_truncated INTEGER NOT NULL DEFAULT 0,
    narration TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS http_work_runs_owner ON http_work_runs(owner_scope, created_ts);
""" % ", ".join("'%s'" % status for status in STATUSES)


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def retention_seconds() -> int:
    return max(3600, min(30 * 86400, _env_int("SONDER_HTTP_WORK_RUN_RETENTION_SECONDS", 7 * 86400)))


def row_limit() -> int:
    return max(16, min(100_000, _env_int("SONDER_HTTP_WORK_RUN_LIMIT", 512)))


def database_path() -> str:
    return sonder_paths.state_path("http_work_runs.db", "SONDER_HTTP_WORK_RUNS_DB")


def _connect() -> sqlite3.Connection:
    path = str(Path(database_path()).expanduser().resolve())
    with _LOCK:
        if path not in _INITIALIZED:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            conn = owned_sqlite_connect(path, timeout=5)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                # Additive migration for databases created before narration.
                _ensure_narration_column(conn)
                conn.commit()
                _ensure_status_vocabulary(conn)
            finally:
                conn.close()
            if os.name != "nt":
                with contextlib.suppress(OSError):
                    os.chmod(path, 0o600)
            _INITIALIZED.add(path)
    conn = owned_sqlite_connect(path, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
    except BaseException:
        conn.close()
        raise
    return conn


def _ensure_narration_column(conn) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(http_work_runs)").fetchall()}
    if "narration" not in columns:
        conn.execute("ALTER TABLE http_work_runs ADD COLUMN narration TEXT NOT NULL DEFAULT '{}'")


_COLUMNS = ("run_id, owner_scope, process_id, status, created_ts, updated_ts, deadline_ts, "
            "cancel_requested, output, output_truncated, narration")


def _ensure_status_vocabulary(conn) -> None:
    """Rebuild a table whose status CHECK predates a status in ``STATUSES``.

    SQLite cannot alter a CHECK constraint, so an older database is copied into
    a table created from the current schema inside one transaction.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'http_work_runs'"
    ).fetchone()
    if row is None or all("'%s'" % status in (row[0] or "") for status in STATUSES):
        return
    try:
        conn.executescript(
            "BEGIN IMMEDIATE;"
            "DROP INDEX IF EXISTS http_work_runs_owner;"
            "ALTER TABLE http_work_runs RENAME TO http_work_runs_old;"
            + _SCHEMA +
            "INSERT INTO http_work_runs(%s) SELECT %s FROM http_work_runs_old;"
            "DROP TABLE http_work_runs_old;"
            "COMMIT;" % (_COLUMNS, _COLUMNS)
        )
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise


_MAX_AWAITING = 16


def _waiting(narration: dict) -> list[dict]:
    """Waiting approvals, oldest first (a legacy single entry is folded in)."""
    found: dict[str, dict] = {}
    entries = narration.get("awaiting_approvals")
    for item in (entries.values() if isinstance(entries, dict) else ()):
        if isinstance(item, dict) and item.get("call_id"):
            found[str(item["call_id"])] = item
    legacy = narration.get("awaiting_approval")
    if isinstance(legacy, dict) and legacy.get("call_id"):
        found.setdefault(str(legacy["call_id"]), legacy)

    def order(item: dict) -> tuple[float, str]:
        try:
            since = float(item.get("since") or 0.0)
        except (TypeError, ValueError):
            since = 0.0
        return (since, str(item["call_id"]))

    return sorted(found.values(), key=order)


def _public(row) -> dict:
    try:
        narration = json.loads(row["narration"] or "{}")
    except (TypeError, ValueError):
        narration = {}
    if not isinstance(narration, dict):
        narration = {}
    waiting = _waiting(narration) if row["status"] == "running" else []
    return {
        "id": row["run_id"],
        "status": row["status"],
        # ``status`` stays ``running`` while a run waits on an operator; the
        # phase names what it is waiting for.  ``awaiting_approval`` is the
        # oldest waiting call, ``awaiting_approvals`` all of them.
        "phase": "awaiting_approval" if waiting else row["status"],
        "awaiting_approval": waiting[0] if waiting else None,
        "awaiting_approvals": waiting,
        "has_output": bool(row["output"]),
        "created_at": row["created_ts"],
        "updated_at": row["updated_ts"],
        "deadline_at": row["deadline_ts"],
        "cancel_requested": bool(row["cancel_requested"]),
        "output": row["output"],
        "output_truncated": bool(row["output_truncated"]),
        "narration": narration,
        "acknowledgement": narration.get("acknowledgement", ""),
        "result_receipt": narration.get("result_receipt", {}),
        "pending_refusal": narration.get("pending_refusal") or None,
    }


def _prune(conn: sqlite3.Connection, now: float) -> None:
    conn.execute(
        "DELETE FROM http_work_runs WHERE status != 'running' AND updated_ts <= ?",
        (now - retention_seconds(),),
    )
    excess = conn.execute("SELECT COUNT(*) FROM http_work_runs").fetchone()[0] - row_limit()
    if excess > 0:
        conn.execute(
            "DELETE FROM http_work_runs WHERE run_id IN ("
            "SELECT run_id FROM http_work_runs WHERE status != 'running' "
            "ORDER BY updated_ts ASC LIMIT ?)",
            (excess,),
        )


def start(run_id: str, *, owner_scope: str, process_id: str, deadline_ts: float,
          now: float | None = None) -> None:
    """Record a newly admitted run as ``running``."""
    if not run_id or not owner_scope or not process_id:
        raise ValueError("run id, owner scope, and process id are required")
    now = time.time() if now is None else float(now)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _prune(conn, now)
        conn.execute(
            "INSERT INTO http_work_runs(run_id, owner_scope, process_id, status, "
            "created_ts, updated_ts, deadline_ts) VALUES (?, ?, ?, 'running', ?, ?, ?)",
            (run_id, owner_scope, process_id, now, now, float(deadline_ts)),
        )
        conn.commit()
    finally:
        conn.close()


def _redact_for_storage(text: str) -> str:
    if not text:
        return ""
    try:
        return redaction.redact_text(text)
    except Exception:
        return redaction.REDACTION_FAILED


def finish(run_id: str, status: str, output: str = "", *, now: float | None = None) -> None:
    """Record the terminal status and bounded answer of a running run."""
    if status not in TERMINAL_STATUSES:
        raise ValueError("terminal work-run status required")
    text = _redact_for_storage(output) if isinstance(output, str) else ""
    truncated = len(text) > MAX_OUTPUT_CHARS
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT narration FROM http_work_runs WHERE run_id = ? AND status = 'running'",
            (run_id,),
        ).fetchone()
        if row is not None:
            try:
                narr = json.loads(row[0] or "{}")
            except (TypeError, ValueError):
                narr = {}
            stale = ["awaiting_approval", "awaiting_approvals"]
            if status in {"returned", "cancelled", "interrupted", "delegated"}:
                stale.append("pending_refusal")
            if isinstance(narr, dict) and any(key in narr for key in stale):
                for key in stale:
                    narr.pop(key, None)
                conn.execute(
                    "UPDATE http_work_runs SET narration = ? WHERE run_id = ?",
                    (json.dumps(narr, separators=(",", ":"), ensure_ascii=True), run_id),
                )
        conn.execute(
            "UPDATE http_work_runs SET status = ?, output = ?, output_truncated = ?, "
            "updated_ts = ? WHERE run_id = ? AND status = 'running'",
            (status, text[:MAX_OUTPUT_CHARS], int(truncated),
             time.time() if now is None else float(now), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _update_narration(run_id: str, mutate) -> None:
    """Apply a bounded narration update without exposing another store.

    ``mutate(value, status)`` receives the narration dict and the row status.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT narration, status FROM http_work_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            return
        try:
            value = json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            value = {}
        value = mutate(value if isinstance(value, dict) else {}, str(row[1]))
        if not isinstance(value, dict):
            raise ValueError("narration mutation must return a mapping")
        conn.execute(
            "UPDATE http_work_runs SET narration = ?, updated_ts = ? WHERE run_id = ?",
            (json.dumps(value, separators=(",", ":"), ensure_ascii=True), time.time(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_narration(
    run_id: str,
    *,
    acknowledgement: str = "",
    activity_id: str = "",
    result_receipt=None,
    pending_refusal: dict | None = None,
    clear_pending_refusal: bool = False,
    awaiting_approval: dict | None = None,
    clear_awaiting_approval: bool | str = False,
) -> None:
    """Persist the owner-visible acknowledgement, optional activity id, and pending refusal."""
    acknowledgement = _redact_for_storage(str(acknowledgement or ""))[:2000]
    activity_id = str(activity_id or "")[:256]

    def mutate(value, status):
        if acknowledgement:
            value["acknowledgement"] = acknowledgement
        if activity_id:
            value["activity_id"] = activity_id
        if isinstance(result_receipt, dict):
            allowed = {"status", "work_run_id", "requested_mode", "routing_reason", "session_ref",
                       "admission_event_id", "return_event_id", "source_event_id", "get_url", "cancel_url",
                       "child_run_id"}
            value["result_receipt"] = {
                key: _redact_for_storage(str(item))[:512]
                for key, item in result_receipt.items()
                if key in allowed
            }
        entries = value.get("awaiting_approvals")
        if not isinstance(entries, dict):
            entries = {}
        legacy = value.pop("awaiting_approval", None)
        if isinstance(legacy, dict) and legacy.get("call_id"):
            entries.setdefault(str(legacy["call_id"])[:256], legacy)
        if clear_awaiting_approval is True:
            entries.clear()
        elif isinstance(clear_awaiting_approval, str) and clear_awaiting_approval:
            entries.pop(clear_awaiting_approval[:256], None)
        elif (status == "running" and isinstance(awaiting_approval, dict)
              and awaiting_approval.get("call_id")):
            key = str(awaiting_approval["call_id"])[:256]
            if key in entries or len(entries) < _MAX_AWAITING:
                entries[key] = {
                    name: str(awaiting_approval[name])[:256]
                    for name in ("call_id", "tool", "since", "wait_seconds")
                    if awaiting_approval.get(name) is not None
                }
        if entries:
            value["awaiting_approvals"] = entries
        else:
            value.pop("awaiting_approvals", None)
        if clear_pending_refusal:
            value.pop("pending_refusal", None)
        elif isinstance(pending_refusal, dict) and pending_refusal:
            kept: dict[str, object] = {}
            for key in ("call_id", "tool", "risk", "mode", "mode_label", "reason"):
                if key in pending_refusal and pending_refusal[key] is not None:
                    kept[key] = _redact_for_storage(str(pending_refusal[key]))[:1000]
            if "detail" in pending_refusal and pending_refusal["detail"] is not None:
                kept["detail"] = _redact_for_storage(str(pending_refusal["detail"]))[:4000]
            for bkey in ("detail_truncated", "bidi_warning"):
                if bkey in pending_refusal:
                    kept[bkey] = bool(pending_refusal[bkey])
            if "detail_chars" in pending_refusal and pending_refusal["detail_chars"] is not None:
                kept["detail_chars"] = int(pending_refusal["detail_chars"])
            if "bidi_chars" in pending_refusal and isinstance(pending_refusal["bidi_chars"], (list, tuple)):
                kept["bidi_chars"] = [str(c)[:16] for c in pending_refusal["bidi_chars"][:16]]
            if "remedies" in pending_refusal and isinstance(pending_refusal["remedies"], list):
                kept["remedies"] = redaction.redact_structure(
                    pending_refusal["remedies"][:8], _redact_for_storage, sensitive_keys=True,
                )
            value["pending_refusal"] = kept
        return value

    _update_narration(run_id, mutate)


def link_run(run_id: str, kind: str, child_id: str) -> None:
    """Attach at most sixteen opaque child ids to an owner-scoped run."""
    kind, child_id = str(kind or "")[:80], str(child_id or "")[:256]
    if not kind or not child_id:
        return

    def mutate(value, _status):
        links = value.get("links", [])
        if not isinstance(links, list):
            links = []
        entry = {"kind": kind, "id": child_id}
        if entry not in links:
            links.append(entry)
        value["links"] = links[-16:]
        return value

    _update_narration(run_id, mutate)


def links(run_id: str) -> list[dict]:
    """The child ids a run linked so far (``[{"kind", "id"}]``), oldest first."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT narration FROM http_work_runs WHERE run_id = ?", (run_id,),
        ).fetchone()
    finally:
        conn.close()
    try:
        value = json.loads(row[0] or "{}") if row is not None else {}
    except (TypeError, ValueError):
        value = {}
    found = value.get("links", []) if isinstance(value, dict) else []
    return [dict(item) for item in found if isinstance(item, dict)]


def request_cancel(run_id: str, *, owner_scope: str) -> dict | None:
    """Set the cancel flag on the caller's run; ``None`` when not theirs."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE http_work_runs SET cancel_requested = 1, updated_ts = ? "
            "WHERE run_id = ? AND owner_scope = ? AND status = 'running'",
            (time.time(), run_id, owner_scope),
        )
        row = conn.execute(
            "SELECT * FROM http_work_runs WHERE run_id = ? AND owner_scope = ?",
            (run_id, owner_scope),
        ).fetchone()
        conn.commit()
        return _public(row) if row is not None else None
    finally:
        conn.close()


def cancel_requested(run_id: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT cancel_requested FROM http_work_runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        # A run whose record vanished cannot prove it may still act.
        return True if row is None else bool(row[0])
    finally:
        conn.close()


def get(run_id: str, *, owner_scope: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM http_work_runs WHERE run_id = ? AND owner_scope = ?",
            (run_id, owner_scope),
        ).fetchone()
        return _public(row) if row is not None else None
    finally:
        conn.close()


def recent(*, owner_scope: str, limit: int = 20) -> list[dict]:
    """The caller's newest runs, without answer text (fetch one run for it)."""
    limit = max(1, min(100, int(limit)))
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM http_work_runs WHERE owner_scope = ? "
            "ORDER BY created_ts DESC LIMIT ?",
            (owner_scope, limit),
        ).fetchall()
    finally:
        conn.close()
    summaries = []
    for row in rows:
        item = _public(row)
        item.pop("output")
        summaries.append(item)
    return summaries


def reconcile(process_id: str, *, now: float | None = None) -> int:
    """Mark runs left ``running`` by any other process as ``interrupted``."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE http_work_runs SET status = 'interrupted', updated_ts = ? "
            "WHERE status = 'running' AND process_id != ?",
            (time.time() if now is None else float(now), process_id),
        ).rowcount
        conn.commit()
        return int(changed or 0)
    finally:
        conn.close()


def reset_for_tests() -> None:
    with _LOCK:
        _INITIALIZED.clear()


__all__ = [
    "MAX_OUTPUT_CHARS", "STATUSES", "TERMINAL_STATUSES", "cancel_requested",
    "database_path", "finish", "get", "recent", "reconcile", "request_cancel",
    "reset_for_tests", "retention_seconds", "row_limit", "start",
    "set_narration", "link_run", "links",
]
