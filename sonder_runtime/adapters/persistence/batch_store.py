"""Durable batch jobs in ``fanout.db``: the job and item records, leases and step states.

A batch job is a scheduler over single-model fanout child runs, so this module
owns only the job layer: the ``batch_jobs`` and ``batch_items`` tables (created
by ``fanout_store``'s one migration path), the job lease, and the read model of
step states joined from the child runs.  Item inputs are vault-sealed by the
caller; no answer is stored here.  Typed domain errors are raised for misuse.

The module itself satisfies ``application.ports.batch_jobs.BatchStorePort``.
"""
from __future__ import annotations

import dataclasses
import json
import os
import socket
import time
import uuid
from typing import Sequence

import sonder_runtime.adapters.persistence.fanout_store as fanout_store
from sonder_runtime.application.ports.batch_jobs import JobRecord, RunSettings
from sonder_runtime.domain.batch_jobs import (
    ACTIVE_JOB_STATUSES, JobSpec, JobStatus, PauseReason, StepReason, StepState, StepStatus,
    spec_from_dict, spec_to_dict,
)
from sonder_runtime.domain.common.errors import CapacityExceeded, Conflict, InvalidInput, NotFound

_ADDABLE = (JobStatus.DRAFT, JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)
_RESUMABLE = (JobStatus.PAUSED, JobStatus.INTERRUPTED, JobStatus.CANCELLED)
_STEP_REASONS = frozenset(reason.value for reason in StepReason)
_SETTING_FIELDS = {field.name for field in dataclasses.fields(RunSettings)}


def _settings(raw: str) -> RunSettings:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        data = {}
    return RunSettings(**{key: value for key, value in data.items() if key in _SETTING_FIELDS})


def _record(row) -> JobRecord:
    return JobRecord(
        id=row["id"], name=row["name"], status=JobStatus(row["status"]), pause_reason=row["pause_reason"],
        spec=spec_from_dict(json.loads(row["spec_json"])), settings=_settings(row["settings_json"]),
        request_owner=row["request_owner"], request_role=row["request_role"],
        segment_start_ts=row["segment_start_ts"], created_ts=row["created_ts"], updated_ts=row["updated_ts"],
        started_ts=row["started_ts"], finished_ts=row["finished_ts"],
        cancel_requested=bool(row["cancel_requested"]),
    )


def _fetch(conn, job_id: str):
    row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (str(job_id),)).fetchone()
    if row is None:
        raise NotFound("batch job was not found")
    return row


def create_job(*, name: str, request_owner: str, request_role: str, spec: JobSpec,
               settings: RunSettings, ttl_seconds: int) -> JobRecord:
    """Persist a new draft job; its spec is already validated."""
    now = time.time()
    job_id = "job-%s" % uuid.uuid4().hex[:16]
    with fanout_store._write_transaction() as conn:
        conn.execute(
            "INSERT INTO batch_jobs(id,name,request_owner,request_role,status,spec_json,settings_json,"
            "ttl_seconds,created_ts,updated_ts) VALUES(?,?,?,?,'draft',?,?,?,?,?)",
            (job_id, fanout_store._safe_text(name, 120), fanout_store._safe_text(request_owner, 200),
             fanout_store._safe_text(request_role, 80), json.dumps(spec_to_dict(spec), ensure_ascii=False),
             json.dumps(dataclasses.asdict(settings)), max(0, int(ttl_seconds)), now, now),
        )
        return _record(_fetch(conn, job_id))


def get_job(job_id: str) -> JobRecord | None:
    """Return the job after reconciling dead owners, or ``None``."""
    fanout_store.reconcile_stale_runs()
    conn = fanout_store._connect()
    try:
        row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (str(job_id),)).fetchone()
        return _record(row) if row is not None else None
    finally:
        conn.close()


def count_active() -> int:
    conn = fanout_store._connect()
    try:
        marks = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        return conn.execute(
            "SELECT COUNT(*) FROM batch_jobs WHERE status IN (%s)" % marks,
            [status.value for status in ACTIVE_JOB_STATUSES],
        ).fetchone()[0]
    finally:
        conn.close()


def add_items(job_id: str, entries: Sequence[tuple[str, str]], *, max_items: int) -> int:
    """Append sealed items ``(key, ciphertext)``; returns the job's item count."""
    now = time.time()
    with fanout_store._write_transaction() as conn:
        job = _fetch(conn, job_id)
        if JobStatus(job["status"]) not in _ADDABLE:
            raise Conflict("items cannot be added to a job in status " + job["status"])
        count, last = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(seq),0) FROM batch_items WHERE job_id=?", (str(job_id),)
        ).fetchone()
        if count + len(entries) > max_items:
            raise InvalidInput("a job has at most %d items" % max_items)
        existing = {row[0] for row in conn.execute("SELECT item_key FROM batch_items WHERE job_id=?", (str(job_id),))}
        for index, (key, ciphertext) in enumerate(entries, start=1):
            if key in existing:
                raise InvalidInput("duplicate item key: " + key)
            existing.add(key)
            conn.execute(
                "INSERT INTO batch_items(job_id,item_key,seq,fields_ciphertext,created_ts) VALUES(?,?,?,?,?)",
                (str(job_id), key, last + index, ciphertext, now),
            )
        conn.execute("UPDATE batch_jobs SET updated_ts=? WHERE id=?", (now, str(job_id)))
        return count + len(entries)


def item_keys(job_id: str) -> list[str]:
    conn = fanout_store._connect()
    try:
        return [row[0] for row in conn.execute(
            "SELECT item_key FROM batch_items WHERE job_id=? ORDER BY seq", (str(job_id),))]
    finally:
        conn.close()


def item_ciphertext(job_id: str, item_key: str) -> str | None:
    conn = fanout_store._connect()
    try:
        row = conn.execute(
            "SELECT fields_ciphertext FROM batch_items WHERE job_id=? AND item_key=?", (str(job_id), str(item_key)),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _enqueue(conn, job_id: str, now: float, extra_settings: dict | None = None) -> None:
    owner = (fanout_store._safe_text("batch-%d" % os.getpid(), 200), os.getpid(), socket.gethostname())
    row = _fetch(conn, job_id)
    settings = json.loads(row["settings_json"] or "{}")
    settings.update(extra_settings or {})
    conn.execute(
        "UPDATE batch_jobs SET status='queued',pause_reason='',cancel_requested=0,finished_ts=NULL,"
        "owner_id=?,owner_pid=?,owner_host=?,lease_until=NULL,settings_json=?,updated_ts=? WHERE id=?",
        (*owner, json.dumps(settings), now, str(job_id)),
    )


def _capacity(conn, max_active: int) -> None:
    marks = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
    active = conn.execute(
        "SELECT COUNT(*) FROM batch_jobs WHERE status IN (%s)" % marks, [s.value for s in ACTIVE_JOB_STATUSES]
    ).fetchone()[0]
    if active >= max_active:
        raise CapacityExceeded("too many active batch jobs (limit %d)" % max_active)


def start_job(job_id: str, *, max_active: int) -> JobRecord:
    """Queue a draft job that has at least one item."""
    now = time.time()
    with fanout_store._write_transaction() as conn:
        row = _fetch(conn, job_id)
        if JobStatus(row["status"]) is not JobStatus.DRAFT:
            raise Conflict("only a draft job can be started")
        if not conn.execute("SELECT 1 FROM batch_items WHERE job_id=? LIMIT 1", (str(job_id),)).fetchone():
            raise Conflict("a job needs at least one item before it starts")
        _capacity(conn, max_active)
        _enqueue(conn, job_id, now)
        return _record(_fetch(conn, job_id))


def resume_job(job_id: str, *, include_failed: bool, max_active: int) -> JobRecord:
    """Queue a paused, interrupted or cancelled job again (a completed one only to retry failures)."""
    now = time.time()
    fanout_store.reconcile_stale_runs(now)
    with fanout_store._write_transaction() as conn:
        status = JobStatus(_fetch(conn, job_id)["status"])
        if not (status in _RESUMABLE or (status is JobStatus.COMPLETED and include_failed)):
            raise Conflict("a job in status %s cannot be resumed" % status.value)
        if status not in ACTIVE_JOB_STATUSES:  # a paused job already counts toward the cap
            _capacity(conn, max_active)
        _enqueue(conn, job_id, now, {"retry_failed": bool(include_failed)})
        return _record(_fetch(conn, job_id))


def request_cancel(job_id: str) -> JobRecord:
    """Cancel a job: at once when no worker owns it, else flag it for the worker."""
    now = time.time()
    with fanout_store._write_transaction() as conn:
        row = _fetch(conn, job_id)
        status = JobStatus(row["status"])
        if status in (JobStatus.COMPLETED, JobStatus.CANCELLED):
            return _record(row)
        if status is JobStatus.RUNNING:
            conn.execute("UPDATE batch_jobs SET cancel_requested=1,updated_ts=? WHERE id=?", (now, str(job_id)))
        else:
            conn.execute(
                "UPDATE batch_jobs SET status='cancelled',cancel_requested=1,owner_id='',owner_pid=0,"
                "owner_host='',lease_until=NULL,finished_ts=?,updated_ts=? WHERE id=?",
                (now, now, str(job_id)),
            )
        return _record(_fetch(conn, job_id))


def active_child_runs(job_id: str) -> list[str]:
    """Child runs that are queued or running; cancel forwards to these."""
    conn = fanout_store._connect()
    try:
        return [row[0] for row in conn.execute(
            "SELECT id FROM fanout_runs WHERE batch_job_id=? AND status IN ('queued','running')", (str(job_id),))]
    finally:
        conn.close()


def claim_job(job_id: str, owner_id: str, owner_pid: int, lease_seconds: int) -> JobRecord | None:
    now = time.time()
    with fanout_store._write_transaction() as conn:
        row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (str(job_id),)).fetchone()
        if row is None or row["status"] != "queued" or row["cancel_requested"]:
            return None
        conn.execute(
            "UPDATE batch_jobs SET status='running',owner_id=?,owner_pid=?,owner_host=?,lease_until=?,"
            "segment_start_ts=?,started_ts=COALESCE(started_ts,?),updated_ts=? WHERE id=?",
            (fanout_store._safe_text(owner_id, 200), int(owner_pid), socket.gethostname(),
             fanout_store._lease(lease_seconds), now, now, now, str(job_id)),
        )
        return _record(_fetch(conn, job_id))


def heartbeat(job_id: str, owner_id: str, lease_seconds: int) -> bool:
    with fanout_store._write_transaction() as conn:
        return conn.execute(
            "UPDATE batch_jobs SET lease_until=?,updated_ts=? WHERE id=? AND owner_id=? AND status='running'",
            (fanout_store._lease(lease_seconds), time.time(), str(job_id), str(owner_id)),
        ).rowcount > 0


def cancel_requested(job_id: str) -> bool:
    conn = fanout_store._connect()
    try:
        row = conn.execute("SELECT cancel_requested FROM batch_jobs WHERE id=?", (str(job_id),)).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def finish(job_id: str, owner_id: str, status: JobStatus, pause_reason: PauseReason | None) -> bool:
    now = time.time()
    terminal = status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.INTERRUPTED)
    with fanout_store._write_transaction() as conn:
        return conn.execute(
            "UPDATE batch_jobs SET status=?,pause_reason=?,owner_id='',owner_pid=0,owner_host='',lease_until=NULL,"
            "finished_ts=?,updated_ts=? WHERE id=? AND owner_id=? AND status='running'",
            (status.value, pause_reason.value if pause_reason else "", now if terminal else None, now,
             str(job_id), str(owner_id)),
        ).rowcount > 0


_STEP_COLUMNS = (
    "SELECT r.id AS run_id, r.batch_stage, r.batch_item_key, s.model, s.status, s.attempts, "
    "s.elapsed_ms, s.tokens_in, s.tokens_out, s.token_source, s.served_model, s.failure_class, "
    "s.answer_truncated, s.answer_truncation_known, s.finished_ts, s.error "
    "FROM fanout_runs r JOIN fanout_results s ON s.run_id=r.id WHERE "
)


def _state(row) -> StepState:
    status = StepStatus(row["status"])
    return StepState(
        stage=row["batch_stage"], item_key=row["batch_item_key"], status=status, run_id=row["run_id"],
        requested_model=row["model"], served_model=row["served_model"] or "",
        tokens_in=row["tokens_in"], tokens_out=row["tokens_out"], token_source=row["token_source"] or "",
        elapsed_ms=row["elapsed_ms"], attempts=row["attempts"] or 0, finished_ts=row["finished_ts"],
        failure_class=(fanout_store.normalize_failure_class(row["failure_class"])
                       if status in (StepStatus.FAILED, StepStatus.UNKNOWN) else None),
        reason=row["error"] if status is StepStatus.FAILED and row["error"] in _STEP_REASONS else "",
        answer_truncated=bool(row["answer_truncated"]),
        answer_truncation_known=bool(row["answer_truncation_known"]),
    )


def step_states(job_id: str) -> dict[tuple[str, str], StepState]:
    """Every step that has a child run, read from its single result row."""
    fanout_store.reconcile_stale_runs()
    conn = fanout_store._connect()
    try:
        rows = conn.execute(_STEP_COLUMNS + "r.batch_job_id=?", (str(job_id),)).fetchall()
    finally:
        conn.close()
    return {(row["batch_stage"], row["batch_item_key"]): _state(row) for row in rows}


def step_state_for_run(run_id: str) -> StepState | None:
    """One child run's step state, or ``None`` when it is not a batch child."""
    conn = fanout_store._connect()
    try:
        row = conn.execute(_STEP_COLUMNS + "r.id=? AND r.batch_job_id<>''", (str(run_id),)).fetchone()
        return _state(row) if row is not None else None
    finally:
        conn.close()


def recent_jobs(*, request_owner: str | None, include_finished: bool, limit: int) -> list[dict]:
    """A content-free job history: ids, states and counts only (no names or inputs)."""
    limit = max(1, min(int(limit), 100))
    fanout_store.reconcile_stale_runs()
    clauses, values = [], []
    if request_owner is not None:
        clauses.append("j.request_owner=?")
        values.append(str(request_owner))
    if not include_finished:
        clauses.append("j.status NOT IN ('completed','cancelled','interrupted')")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = fanout_store._connect()
    try:
        rows = conn.execute(
            "SELECT j.id AS job_id, j.status, j.pause_reason, j.created_ts, j.updated_ts, j.finished_ts, "
            "(SELECT COUNT(*) FROM batch_items i WHERE i.job_id=j.id) AS items "
            "FROM batch_jobs j" + where + " ORDER BY j.updated_ts DESC LIMIT ?", (*values, limit)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
