"""Durable, process-safe receipts for bounded model fanout.

This module owns only ``fanout.db``.  It deliberately does not call models or
import the server: callers claim a run/result, perform the expensive work, and
then record a bounded receipt.  That separation makes an interrupted process
recoverable without replaying an unknown cloud request.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import sonder_paths
from sonder_runtime.adapters.process_liveness import pid_alive as _process_pid_alive

MAX_PROMPT_CHARS = 16_000
MAX_ANSWER_CHARS = 64_000
MAX_ERROR_CHARS = 4_000
MAX_EVENT_CHARS = 1_000
MAX_MODELS = 128
DEFAULT_LEASE_SECONDS = 300
ACTIVE_RUNS = frozenset(("queued", "running", "cancelling"))
TERMINAL_RUNS = frozenset(("completed", "cancelled", "interrupted"))
RESULT_STATUSES = frozenset(("pending", "running", "answered", "failed", "skipped", "unknown"))
_SCHEMA_LOCK = threading.RLock()
_INITIALIZED_PATHS: set[str] = set()

# Intentionally conservative: the database is a receipt, not a secret store.
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")
_URI_CREDENTIAL = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+(?::[^\s/@]+)?@")
_SENSITIVE_NAME = r"(?:api[_-]?key|access[_-]?key|authorization|token|secret|password|[a-z][a-z0-9_-]{0,40}_(?:token|secret|password))"
_ASSIGNMENT = re.compile(r"(?i)\b(" + _SENSITIVE_NAME + r")\s*[:=]\s*([^\s,;]{4,})")
_SENSITIVE_ASSIGNMENT = re.compile(r"(?i)\b(" + _SENSITIVE_NAME + r")\s*[:=]\s*([^\s,;]{4,})")
_DOUBLE_QUOTED_ASSIGNMENT = re.compile(r"(?i)([\"']?" + _SENSITIVE_NAME + r"[\"']?\s*[:=]\s*)\"(?:\\.|[^\"])*\"")
_SINGLE_QUOTED_ASSIGNMENT = re.compile(r"(?i)([\"']?" + _SENSITIVE_NAME + r"[\"']?\s*[:=]\s*)'(?:\\.|[^'])*'")
_KEY = re.compile(r"\b(?:sk|rk|ghp)_[A-Za-z0-9_-]{16,}\b")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fanout_runs (
 id TEXT PRIMARY KEY, request_owner TEXT NOT NULL DEFAULT '', request_role TEXT NOT NULL DEFAULT '',
 prompt TEXT NOT NULL, prompt_sha256 TEXT NOT NULL, models_json TEXT NOT NULL,
 execution_prompt_ciphertext TEXT NOT NULL DEFAULT '',
 scope TEXT NOT NULL DEFAULT 'local', cloud_opt_in INTEGER NOT NULL DEFAULT 0,
 limits_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
 owner_id TEXT NOT NULL DEFAULT '', owner_pid INTEGER NOT NULL DEFAULT 0, owner_host TEXT NOT NULL DEFAULT '',
 lease_until REAL, created_ts REAL NOT NULL, updated_ts REAL NOT NULL, finished_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_fanout_runs_status ON fanout_runs(status, updated_ts DESC);
CREATE TABLE IF NOT EXISTS fanout_results (
 run_id TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 owner_id TEXT NOT NULL DEFAULT '', owner_pid INTEGER NOT NULL DEFAULT 0, owner_host TEXT NOT NULL DEFAULT '',
 lease_until REAL, answer TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', elapsed_ms INTEGER,
 retry_after_ts REAL, started_ts REAL, finished_ts REAL, updated_ts REAL NOT NULL,
 PRIMARY KEY(run_id, model), FOREIGN KEY(run_id) REFERENCES fanout_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fanout_results_claim ON fanout_results(run_id, status, updated_ts);
CREATE TABLE IF NOT EXISTS fanout_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts REAL NOT NULL,
 kind TEXT NOT NULL, message TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES fanout_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fanout_events_run ON fanout_events(run_id, event_id DESC);
CREATE TABLE IF NOT EXISTS model_health (
 model TEXT PRIMARY KEY, model_class TEXT NOT NULL DEFAULT '', failure_count INTEGER NOT NULL DEFAULT 0,
 availability_failure_count INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT '', disabled_until REAL, last_success_ts REAL, updated_ts REAL NOT NULL
);
"""


def database_path() -> str:
    return sonder_paths.state_path("fanout.db", "SONDER_FANOUT_DB")


def _safe_text(value, limit: int) -> str:
    text = str(value or "")
    text = _BEARER.sub("Bearer <redacted>", text)
    text = _URI_CREDENTIAL.sub(r"\1<redacted>@", text)
    # Avoid repeatedly running field-name patterns across megabyte-scale model
    # output that has none of the relevant markers.
    lower = text.casefold()
    if any(marker in lower for marker in ("token", "secret", "password", "api_key", "api-key", "access_key", "access-key", "authorization")):
        text = _DOUBLE_QUOTED_ASSIGNMENT.sub(r"\1<redacted>", text)
        text = _SINGLE_QUOTED_ASSIGNMENT.sub(r"\1<redacted>", text)
        text = _ASSIGNMENT.sub(lambda m: "%s=<redacted>" % m.group(1), text)
        text = _SENSITIVE_ASSIGNMENT.sub(lambda m: "%s=<redacted>" % m.group(1), text)
    text = _KEY.sub("<redacted-key>", text)
    return _CONTROL.sub("?", text)[:limit]


def _json(value: object, limit: int = 32_000) -> str:
    import json
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text) > limit:
        raise ValueError("fanout JSON state exceeds %d characters" % limit)
    return text


def _ensure_schema(path: str) -> None:
    resolved = str(Path(path).expanduser().resolve())
    with _SCHEMA_LOCK:
        if resolved in _INITIALIZED_PATHS:
            return
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)
        # ``journal_mode=WAL`` requires a database-level lock of its own. Two
        # processes opening a pre-WAL legacy receipt can race *before* the
        # BEGIN IMMEDIATE below. Bound and retry that transient lock instead of
        # treating a concurrent upgrade as a failed runtime startup.
        deadline = time.monotonic() + 5.0
        while True:
            retry = False
            conn = sqlite3.connect(resolved, timeout=5)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                # The process-local lock above is not enough for two live runtime
                # processes upgrading the same durable fanout.db. Take SQLite's
                # cross-process write lock before every probe-and-ALTER sequence.
                conn.execute("BEGIN IMMEDIATE")
                # Existing receipt databases predate the sealed execution payload.
                # Migration is additive and the ciphertext is never exposed by the
                # public receipt readers below.
                columns = {row[1] for row in conn.execute("PRAGMA table_info(fanout_runs)")}
                if "execution_prompt_ciphertext" not in columns:
                    conn.execute(
                        "ALTER TABLE fanout_runs ADD COLUMN execution_prompt_ciphertext TEXT NOT NULL DEFAULT ''"
                    )
                health_columns = {row[1] for row in conn.execute("PRAGMA table_info(model_health)")}
                if "availability_failure_count" not in health_columns:
                    # ``failure_count`` historically included prompt/configuration
                    # rejects. Do not copy it: a fresh zero is the only safe value
                    # for an availability-only cooldown counter.
                    conn.execute(
                        "ALTER TABLE model_health ADD COLUMN availability_failure_count INTEGER NOT NULL DEFAULT 0"
                    )
                # Before sealed prompts existed this database retained a best-effort
                # redacted copy plus a stable digest.  That is not an acceptable
                # durable prompt store: a redactor cannot safely classify arbitrary
                # private prose, and a digest enables offline guessing.  Old runs
                # cannot be resumed without their sealed payload, so scrub them in
                # the same migration that introduces the vault column.
                conn.execute(
                    "UPDATE fanout_runs SET prompt='legacy-fanout-prompt:redacted', "
                    "prompt_sha256='' WHERE COALESCE(execution_prompt_ciphertext, '')='' "
                    "AND prompt NOT LIKE 'legacy-fanout-prompt:%'"
                )
                conn.commit()
            except sqlite3.OperationalError as exc:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                # SQLite's messages are stable across supported Python builds;
                # retry only contention, never malformed schemas or I/O faults.
                retry = (
                    ("locked" in str(exc).lower() or "busy" in str(exc).lower())
                    and time.monotonic() < deadline
                )
                if not retry:
                    raise
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            if not retry:
                break
            time.sleep(0.05)
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(resolved, 0o600)
        _INITIALIZED_PATHS.add(resolved)


def _connect() -> sqlite3.Connection:
    path = database_path(); _ensure_schema(path)
    conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000"); conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextlib.contextmanager
def _write_transaction():
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def _row(row) -> dict | None:
    if row is None: return None
    result = dict(row)
    result.pop("execution_prompt_ciphertext", None)
    for key in ("cloud_opt_in", "cancel_requested"):
        if key in result: result[key] = bool(result[key])
    return result


def _event(conn, run_id: str, kind: str, message: str, now: float) -> None:
    conn.execute("INSERT INTO fanout_events(run_id,ts,kind,message) VALUES(?,?,?,?)",
                 (run_id, now, _safe_text(kind, 40), _safe_text(message, MAX_EVENT_CHARS)))


def _lease(seconds: int, max_seconds: int = 3600) -> float:
    return time.time() + max(30, min(int(seconds), max_seconds))


def create_run(prompt: str, models, *, request_owner: str = "", request_role: str = "",
               scope: str = "local", cloud_opt_in: bool = False, limits=None,
               execution_prompt_ciphertext: str = "") -> dict:
    clean_models = []
    for value in models or ():
        name = _safe_text(value, 200).strip()
        if name and name not in clean_models: clean_models.append(name)
    if not clean_models: raise ValueError("fanout requires at least one model")
    if len(clean_models) > MAX_MODELS: raise ValueError("fanout has too many models")
    raw_prompt = str(prompt or "")
    if not raw_prompt.strip(): raise ValueError("fanout prompt is required")
    if len(raw_prompt) > MAX_PROMPT_CHARS: raise ValueError("fanout prompt exceeds %d characters" % MAX_PROMPT_CHARS)
    ciphertext = str(execution_prompt_ciphertext or "")
    if len(ciphertext) > 64_000:
        raise ValueError("fanout sealed prompt exceeds 64000 characters")
    now = time.time(); run_id = "fan-%s" % uuid.uuid4().hex[:16]
    stored_prompt = _safe_text(raw_prompt, MAX_PROMPT_CHARS)
    with _write_transaction() as conn:
        conn.execute("""INSERT INTO fanout_runs(id,request_owner,request_role,prompt,prompt_sha256,models_json,execution_prompt_ciphertext,scope,cloud_opt_in,limits_json,status,created_ts,updated_ts)
                        VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                     (run_id, _safe_text(request_owner, 200), _safe_text(request_role, 80), stored_prompt,
                      hashlib.sha256(raw_prompt.encode("utf-8")).hexdigest(), _json(clean_models),
                      ciphertext, _safe_text(scope, 40),
                      int(bool(cloud_opt_in)), _json(limits or {}), now, now))
        conn.executemany("INSERT INTO fanout_results(run_id,model,status,updated_ts) VALUES(?,?,'pending',?)",
                         [(run_id, model, now) for model in clean_models])
        _event(conn, run_id, "created", "fanout receipt created for %d models" % len(clean_models), now)
        row = conn.execute("SELECT * FROM fanout_runs WHERE id=?", (run_id,)).fetchone()
    return _row(row)


def get_run(run_id: str) -> dict | None:
    reconcile_stale_runs()
    conn = _connect()
    try: return _row(conn.execute("SELECT * FROM fanout_runs WHERE id=?", (str(run_id),)).fetchone())
    finally: conn.close()


def execution_prompt_ciphertext(run_id: str) -> str | None:
    """Return the sealed prompt only to the server-side execution worker."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT execution_prompt_ciphertext FROM fanout_runs WHERE id=?",
            (str(run_id),),
        ).fetchone()
        return str(row["execution_prompt_ciphertext"] or "") if row else None
    finally:
        conn.close()


def list_runs(*, include_finished: bool = True, limit: int = 20) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    conn = _connect()
    try:
        query = "SELECT * FROM fanout_runs"
        if not include_finished:
            query += " WHERE status NOT IN ('completed','cancelled','interrupted')"
        return [_row(r) for r in conn.execute(query + " ORDER BY updated_ts DESC LIMIT ?", (limit,))]
    finally: conn.close()


def list_results(run_id: str, *, include_answers: bool = True) -> list[dict]:
    conn = _connect()
    try:
        columns = "*" if include_answers else "run_id,model,status,attempts,elapsed_ms,retry_after_ts,started_ts,finished_ts,updated_ts"
        return [_row(r) for r in conn.execute("SELECT %s FROM fanout_results WHERE run_id=? ORDER BY model" % columns, (str(run_id),))]
    finally: conn.close()


def events(run_id: str, limit: int = 50) -> list[dict]:
    conn = _connect(); limit = max(1, min(int(limit), 200))
    try:
        rows = conn.execute("SELECT event_id,run_id,ts,kind,message FROM fanout_events WHERE run_id=? ORDER BY event_id DESC LIMIT ?", (str(run_id), limit)).fetchall()
        return [dict(r) for r in reversed(rows)]
    finally: conn.close()


def claim_run(run_id: str, owner_id: str, *, owner_pid: int, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict | None:
    now = time.time(); lease = _lease(lease_seconds)
    with _write_transaction() as conn:
        row = conn.execute("SELECT * FROM fanout_runs WHERE id=?", (str(run_id),)).fetchone()
        if row is None or row["status"] in TERMINAL_RUNS or row["cancel_requested"]: return None
        if row["owner_id"] and row["owner_id"] != owner_id and row["lease_until"] and row["lease_until"] >= now: return None
        if row["owner_id"] and row["owner_id"] != owner_id:
            # Lease ownership is a run-wide fence.  A successor must atomically
            # retire any old in-flight result before taking the run, otherwise
            # a delayed prior worker can commit after its lease has ended.
            conn.execute("UPDATE fanout_results SET status='unknown',error='worker lease transferred; request was not replayed',finished_ts=?,updated_ts=?,owner_id='',owner_pid=0,owner_host='',lease_until=NULL WHERE run_id=? AND status='running'", (now, now, str(run_id)))
        changed = conn.execute("UPDATE fanout_runs SET status='running',owner_id=?,owner_pid=?,owner_host=?,lease_until=?,updated_ts=? WHERE id=? AND status NOT IN ('completed','cancelled','interrupted') AND cancel_requested=0",
                               (_safe_text(owner_id, 200), int(owner_pid), socket.gethostname(), lease, now, str(run_id))).rowcount
        if not changed: return None
        _event(conn, str(run_id), "claimed", "fanout claimed by local worker", now)
        return _row(conn.execute("SELECT * FROM fanout_runs WHERE id=?", (str(run_id),)).fetchone())


def claim_next_result(run_id: str, owner_id: str, *, owner_pid: int, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict | None:
    now = time.time(); lease = _lease(lease_seconds)
    with _write_transaction() as conn:
        run = conn.execute("SELECT * FROM fanout_runs WHERE id=?", (str(run_id),)).fetchone()
        if (run is None or run["owner_id"] != owner_id or run["status"] != "running"
                or run["cancel_requested"] or run["lease_until"] is None or run["lease_until"] < now): return None
        candidate = conn.execute("SELECT model FROM fanout_results WHERE run_id=? AND status='pending' AND (retry_after_ts IS NULL OR retry_after_ts<=?) ORDER BY model LIMIT 1", (str(run_id), now)).fetchone()
        if candidate is None: return None
        updated = conn.execute("UPDATE fanout_results SET status='running',attempts=attempts+1,owner_id=?,owner_pid=?,owner_host=?,lease_until=?,started_ts=?,updated_ts=? WHERE run_id=? AND model=? AND status='pending'",
                               (_safe_text(owner_id, 200), int(owner_pid), socket.gethostname(), lease, now, now, str(run_id), candidate["model"])).rowcount
        if not updated: return None
        return _row(conn.execute("SELECT * FROM fanout_results WHERE run_id=? AND model=?", (str(run_id), candidate["model"])).fetchone())


def record_result(run_id: str, model: str, owner_id: str, status: str, *, answer: str = "", error: str = "", elapsed_ms: int | None = None, retry_after_ts: float | None = None) -> dict | None:
    if status not in ("answered", "failed", "skipped", "unknown"): raise ValueError("invalid fanout result status: %s" % status)
    now = time.time(); elapsed = None if elapsed_ms is None else max(0, min(int(elapsed_ms), 86_400_000))
    with _write_transaction() as conn:
        run = conn.execute("SELECT cancel_requested,owner_id,status,lease_until FROM fanout_runs WHERE id=?", (str(run_id),)).fetchone()
        if (run is None or run["cancel_requested"] or run["owner_id"] != owner_id
                or run["status"] != "running" or run["lease_until"] is None
                or run["lease_until"] < now): return None
        changed = conn.execute("""UPDATE fanout_results SET status=?,answer=?,error=?,elapsed_ms=?,retry_after_ts=?,finished_ts=?,updated_ts=?,owner_id='',owner_pid=0,owner_host='',lease_until=NULL
                               WHERE run_id=? AND model=? AND owner_id=? AND status='running'""",
                               (status, _safe_text(answer, MAX_ANSWER_CHARS), _safe_text(error, MAX_ERROR_CHARS), elapsed, retry_after_ts, now, now, str(run_id), str(model), str(owner_id))).rowcount
        if not changed: return None
        _event(conn, str(run_id), status, "%s: %s" % (model, error or status), now)
        _finish_if_done(conn, str(run_id), now)
        return _row(conn.execute("SELECT * FROM fanout_results WHERE run_id=? AND model=?", (str(run_id), str(model))).fetchone())


def _finish_if_done(conn, run_id: str, now: float) -> None:
    remaining = conn.execute("SELECT COUNT(*) FROM fanout_results WHERE run_id=? AND status IN ('pending','running')", (run_id,)).fetchone()[0]
    if not remaining:
        conn.execute("UPDATE fanout_runs SET status='completed',owner_id='',owner_pid=0,owner_host='',lease_until=NULL,finished_ts=?,updated_ts=? WHERE id=? AND status='running'", (now, now, run_id))
        _event(conn, run_id, "completed", "all model receipts are terminal", now)


def request_cancel(run_id: str) -> dict | None:
    now = time.time()
    with _write_transaction() as conn:
        row = conn.execute("SELECT * FROM fanout_runs WHERE id=?", (str(run_id),)).fetchone()
        if row is None: return None
        if row["status"] in TERMINAL_RUNS: return _row(row)
        conn.execute("UPDATE fanout_results SET status='skipped',finished_ts=?,updated_ts=?,owner_id='',owner_pid=0,owner_host='',lease_until=NULL WHERE run_id=? AND status IN ('pending','running')", (now, now, str(run_id)))
        conn.execute("UPDATE fanout_runs SET status='cancelled',cancel_requested=1,owner_id='',owner_pid=0,owner_host='',lease_until=NULL,finished_ts=?,updated_ts=? WHERE id=?", (now, now, str(run_id)))
        message = "fanout cancelled; late model receipts will be discarded"
        _event(conn, str(run_id), "cancel", message, now)
        return _row(conn.execute("SELECT * FROM fanout_runs WHERE id=?", (str(run_id),)).fetchone())


def resume_run(run_id: str, *, include_failed: bool = False, retry_unknown: bool = False) -> dict | None:
    """Requeue selected terminal receipts; unknown requests require explicit opt-in.

    The worker must claim the resulting queued run before any call is made.
    """
    now = time.time()
    statuses = ["skipped"]
    if include_failed:
        statuses.append("failed")
    if retry_unknown:
        statuses.append("unknown")
    marks = ",".join("?" for _ in statuses)
    with _write_transaction() as conn:
        row = conn.execute("SELECT * FROM fanout_runs WHERE id=?", (str(run_id),)).fetchone()
        if row is None or row["status"] not in TERMINAL_RUNS:
            return None
        changed = conn.execute("UPDATE fanout_results SET status='pending',answer='',error='',elapsed_ms=NULL,retry_after_ts=NULL,started_ts=NULL,finished_ts=NULL,owner_id='',owner_pid=0,owner_host='',lease_until=NULL,updated_ts=? WHERE run_id=? AND status IN (%s)" % marks, (now, str(run_id), *statuses)).rowcount
        if not changed:
            return None
        conn.execute("UPDATE fanout_runs SET status='queued',cancel_requested=0,owner_id='',owner_pid=0,owner_host='',lease_until=NULL,finished_ts=NULL,updated_ts=? WHERE id=?", (now, str(run_id)))
        _event(conn, str(run_id), "resumed", "requeued %d model receipts" % changed, now)
        return _row(conn.execute("SELECT * FROM fanout_runs WHERE id=?", (str(run_id),)).fetchone())


def heartbeat(run_id: str, owner_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
    with _write_transaction() as conn:
        return conn.execute("UPDATE fanout_runs SET lease_until=?,updated_ts=? WHERE id=? AND owner_id=? AND status='running'", (_lease(lease_seconds), time.time(), str(run_id), str(owner_id))).rowcount > 0


def reconcile_stale_runs(now: float | None = None) -> int:
    current = time.time() if now is None else float(now); host = socket.gethostname(); changed = 0
    with _write_transaction() as conn:
        runs = conn.execute("SELECT * FROM fanout_runs WHERE status IN ('queued','running','cancelling')").fetchall()
        for run in runs:
            stale = run["status"] != "queued" and (run["lease_until"] is None or run["lease_until"] < current or (run["owner_host"] == host and not _process_pid_alive(run["owner_pid"])))
            if not stale: continue
            conn.execute("UPDATE fanout_results SET status='unknown',error='worker lease ended; request was not replayed',finished_ts=?,updated_ts=?,owner_id='',owner_pid=0,owner_host='',lease_until=NULL WHERE run_id=? AND status='running'", (current, current, run["id"]))
            conn.execute("UPDATE fanout_runs SET status='interrupted',owner_id='',owner_pid=0,owner_host='',lease_until=NULL,finished_ts=?,updated_ts=? WHERE id=?", (current, current, run["id"]))
            _event(conn, run["id"], "interrupted", "worker lease ended; explicit resume is required", current); changed += 1
    return changed


def record_model_health(model: str, *, model_class: str = "", success: bool = False,
                        error: str = "", disabled_until: float | None = None,
                        counts_toward_backoff: bool = True) -> dict:
    now = time.time(); model = _safe_text(model, 200).strip()
    if not model: raise ValueError("model is required")
    with _write_transaction() as conn:
        existing = conn.execute("SELECT * FROM model_health WHERE model=?", (model,)).fetchone()
        if success:
            conn.execute("INSERT INTO model_health(model,model_class,failure_count,availability_failure_count,last_error,disabled_until,last_success_ts,updated_ts) VALUES(?,?,0,0,'',NULL,?,?) ON CONFLICT(model) DO UPDATE SET model_class=excluded.model_class,failure_count=0,availability_failure_count=0,last_error='',disabled_until=NULL,last_success_ts=excluded.last_success_ts,updated_ts=excluded.updated_ts", (model, _safe_text(model_class, 40), now, now))
        else:
            increment = bool(counts_toward_backoff)
            conn.execute(
                "INSERT INTO model_health(model,model_class,failure_count,availability_failure_count,last_error,disabled_until,updated_ts) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(model) DO UPDATE SET model_class=excluded.model_class,"
                "failure_count=model_health.failure_count+1,"
                "availability_failure_count=CASE WHEN ? THEN model_health.availability_failure_count+1 ELSE model_health.availability_failure_count END,"
                "last_error=excluded.last_error,disabled_until=excluded.disabled_until,updated_ts=excluded.updated_ts",
                (model, _safe_text(model_class, 40), 1, 1 if increment else 0,
                 _safe_text(error, MAX_ERROR_CHARS), disabled_until, now, increment),
            )
        return _row(conn.execute("SELECT * FROM model_health WHERE model=?", (model,)).fetchone())


def get_model_health(model: str) -> dict | None:
    conn = _connect()
    try: return _row(conn.execute("SELECT * FROM model_health WHERE model=?", (str(model),)).fetchone())
    finally: conn.close()


def prune(*, ttl_seconds: int = 7 * 86_400, event_ttl_seconds: int | None = None, now: float | None = None) -> dict:
    current = time.time() if now is None else float(now); cutoff = current - max(60, int(ttl_seconds)); event_cutoff = current - max(60, int(event_ttl_seconds or ttl_seconds))
    with _write_transaction() as conn:
        events_deleted = conn.execute("DELETE FROM fanout_events WHERE ts<?", (event_cutoff,)).rowcount
        runs_deleted = conn.execute("DELETE FROM fanout_runs WHERE status IN ('completed','cancelled','interrupted') AND updated_ts<?", (cutoff,)).rowcount
        health_deleted = conn.execute("DELETE FROM model_health WHERE updated_ts<? AND (disabled_until IS NULL OR disabled_until<?)", (cutoff, current)).rowcount
    return {"runs": runs_deleted, "events": events_deleted, "health": health_deleted}


def reset_schema_cache_for_tests() -> None:
    with _SCHEMA_LOCK: _INITIALIZED_PATHS.clear()
