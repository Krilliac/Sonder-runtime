"""Registrar for the durable batch-job MCP tools.

A batch job runs N items through K local-model stages as single-model durable
fanout child runs (see ``batch_job_executor``), persisted in ``fanout.db`` so it
resumes after a restart, with progress, ETA, per-item and per-job tokens, and
the model that actually answered each step.  One daemon worker per process runs
queued jobs in FIFO order: the local GPU is serial.

``runtime`` is a zero-argument provider of the server module (the convention
registrars follow); it supplies the developer gate, owner identity, the model
catalog and the fanout executor, and is resolved per request so registering
never touches the host.  Tool errors are ``{"ok": false, "error": {"code",
"message"}}`` built from ``SonderError`` codes.
"""
import dataclasses
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Protocol, TypeAlias, TypeVar, cast

from sonder_runtime.adapters.persistence import batch_store, fanout_store
from sonder_runtime.application.batch_jobs.runner import run_job
from sonder_runtime.application.ports.batch_jobs import JobRecord, RunSettings
from sonder_runtime.application.ports.runtime_threads import Thread as owned_runtime_thread
from sonder_runtime.bootstrap.batch_job_executor import ExecutorRuntime, FanoutStepExecutor
from sonder_runtime.bootstrap.config_loading import load_config_or_none
from sonder_runtime.domain.batch_jobs import (
    ItemSpec, JobSpec, JobStatus, Limits, StepState, StepStatus, effective_statuses, receipt_body, step_entry,
    token_rollup, validate_items, validate_spec,
)
from sonder_runtime.domain.common.errors import (
    DependencyUnavailable, Forbidden, InternalFailure, InvalidInput, NotFound, SonderError,
)
from sonder_runtime.platform.batch_jobs_config import BatchJobsConfig

logger = logging.getLogger(__name__)

NOT_FOUND_MESSAGE = "batch job was not found"
MAX_RESULTS_PAGE = 200

_F = TypeVar("_F", bound=Callable[..., object])


class _McpServer(Protocol):
    """The one method of the MCP server object that ``register`` uses."""

    def tool(self) -> Callable[[_F], _F]: ...


class ToolsRuntime(ExecutorRuntime, Protocol):
    """The server-module surface the tool bodies use (the server module or a test double)."""

    def _developer_gate(self, tool_name: str, token: str, started: float) -> str | None: ...

    def _direct_fanout_identity(self, token: str) -> tuple[str, Mapping[str, Any] | None]: ...

    def _deployment_authenticates_callers(self) -> bool: ...

    def resolve_discovered_model_record(self, selector: str) -> tuple[str, object] | None: ...

    def _fanout_nonchat_reason(self, record: object) -> str: ...

    def _is_cloud_model_name(self, model: str) -> bool: ...


def _config() -> BatchJobsConfig:
    config = load_config_or_none()
    return config.batch_jobs if config is not None else BatchJobsConfig()


def _limits(config: BatchJobsConfig) -> Limits:
    return Limits(
        max_items_per_job=config.max_items_per_job, max_stages=config.max_stages,
        max_items_per_add=config.max_items_per_add, max_item_input_chars=config.max_item_input_chars,
        max_rendered_prompt_chars=config.max_rendered_prompt_chars,
        default_item_timeout_seconds=config.default_item_timeout_seconds,
        max_item_timeout_seconds=config.max_item_timeout_seconds,
    )


def _settings(config: BatchJobsConfig) -> RunSettings:
    return RunSettings(
        order=config.order, max_consecutive_failures=config.max_consecutive_failures,
        eta_window=config.eta_window, eta_min_samples=config.eta_min_samples,
        max_rendered_prompt_chars=config.max_rendered_prompt_chars,
        answer_max_chars=config.answer_max_chars,
        max_item_timeout_seconds=config.max_item_timeout_seconds,
    )


def error_payload(error: SonderError) -> dict[str, Any]:
    return {"ok": False, "error": {"code": error.code, "message": str(error)}}


def _plain_int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidInput("%s must be an integer in %d..%d" % (name, low, high))
    return value


def _plain_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidInput("%s must be a boolean" % name)
    return value


def _job_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise InvalidInput("job_id must be a non-empty string")
    return value.strip()


class BatchWorker:
    """One daemon thread running queued job ids in order; started on first submit."""

    def __init__(self, run_one: Callable[[str], None]) -> None:
        self._run_one = run_one
        self._queue: deque[str] = deque()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._busy = False

    def submit(self, job_id: str) -> None:
        with self._cond:
            self._queue.append(job_id)
            if self._thread is None or not self._thread.is_alive():
                self._thread = cast("Callable[..., threading.Thread]", owned_runtime_thread)(
                    target=self._loop, name="batch-job-worker", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._busy = False
                    self._cond.notify_all()
                    if not self._cond.wait(timeout=30) and not self._queue:
                        self._thread = None
                        return
                job_id = self._queue.popleft()
                self._busy = True
            try:
                self._run_one(job_id)
            except Exception:  # noqa: BLE001 - the worker must outlive any one job
                logger.exception("batch job worker failed on %s", job_id)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until the queue is empty and no job is running (used by tests and shutdown)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
        return True


class BatchJobService:
    """The tool bodies; each returns a plain dict and raises ``SonderError`` for misuse."""

    def __init__(self, runtime: Callable[[], ToolsRuntime] | None, record: Callable[..., object] | None) -> None:
        if runtime is not None and not callable(runtime):
            raise TypeError("runtime must be a callable returning the runtime object, got %s" % type(runtime).__name__)
        self._runtime_provider = runtime
        self._record = record
        self._executor: FanoutStepExecutor | None = None
        self.worker = BatchWorker(self._run_one)

    # -- plumbing -----------------------------------------------------------
    def _rt(self) -> ToolsRuntime:
        if self._runtime_provider is None:
            raise TypeError("no runtime provider was supplied")
        return self._runtime_provider()

    def executor(self) -> FanoutStepExecutor:
        if self._executor is None:
            self._executor = FanoutStepExecutor(self._rt())
        return self._executor

    def _run_one(self, job_id: str) -> None:
        owner_id = self._rt()._fanout_worker_id()
        try:
            run_job(job_id, store=batch_store, executor=self.executor(), owner_id=owner_id, owner_pid=os.getpid())
        except Exception:  # noqa: BLE001 - leave the job resumable, never half-owned
            logger.exception("batch job %s stopped on an unexpected error", job_id)
            batch_store.finish(job_id, owner_id, JobStatus.INTERRUPTED, None)

    def _enabled(self) -> BatchJobsConfig:
        config = _config()
        if not config.enabled:
            raise Forbidden("batch jobs are disabled by configuration")
        return config

    def _gate(self, name: str, token: str, started: float) -> None:
        refusal = self._rt()._developer_gate(name, token, started)
        if refusal:
            raise Forbidden(refusal)

    def _owner(self, token: str) -> tuple[str, str]:
        owner, account = self._rt()._direct_fanout_identity(token)
        return owner, str((account or {}).get("role") or "local-open")

    def _authorized(self, job_id: object, token: str) -> JobRecord:
        """The job, or one ``NotFound`` for a missing job and another caller's job alike."""
        job = batch_store.get_job(_job_id(job_id))
        if job is None:
            raise NotFound(NOT_FOUND_MESSAGE)
        rt = self._rt()
        if not rt._deployment_authenticates_callers():
            return job
        owner, account = rt._direct_fanout_identity(token)
        if str((account or {}).get("role") or "") == "admin" or job.request_owner == owner:
            return job
        raise NotFound(NOT_FOUND_MESSAGE)

    def _resolve_models(self, spec: JobSpec) -> JobSpec:
        """Replace each stage model with its canonical installed name; refuse non-chat models."""
        rt = self._rt()
        stages = []
        for stage in spec.stages:
            try:
                found = rt.resolve_discovered_model_record(stage.model)
            except (OSError, TimeoutError) as exc:
                raise DependencyUnavailable("the model catalog is unavailable") from exc
            if not found:
                raise InvalidInput("stage %s model is not installed: %s" % (stage.name, stage.model))
            if rt._fanout_nonchat_reason(found[1]):
                raise InvalidInput("stage %s model cannot chat: %s" % (stage.name, stage.model))
            if rt._is_cloud_model_name(found[0]):
                raise InvalidInput("batch jobs run local models only; refused cloud model for stage " + stage.name)
            stages.append(dataclasses.replace(stage, model=found[0]))
        return dataclasses.replace(spec, stages=tuple(stages))

    @staticmethod
    def _abandon_draft(job_id: str, exc: Exception) -> SonderError:
        """Cancel the draft a failed create left behind; the error to raise names its id."""
        try:
            batch_store.request_cancel(job_id)
        except Exception:  # noqa: BLE001 - the original failure is the one to report
            logger.exception("could not cancel the draft batch job %s", job_id)
        if isinstance(exc, SonderError):
            return type(exc)("%s (job %s was cancelled)" % (exc, job_id))
        logger.error("batch job %s could not be filled or started", job_id, exc_info=exc)
        return InternalFailure("batch job tool failed; job %s was cancelled" % job_id)

    def _seal_items(self, items: Iterable[ItemSpec]) -> list[tuple[str, str]]:
        executor = self.executor()
        return [(item.key, executor.seal_fields(item.fields)) for item in items]

    def _current(self, job_id: str) -> JobRecord:
        """The job as stored now; a job that has just been written is always there."""
        job = batch_store.get_job(job_id)
        if job is None:
            raise NotFound(NOT_FOUND_MESSAGE)
        return job

    def _receipt(self, job: JobRecord) -> dict[str, Any]:
        keys = batch_store.item_keys(job.id)
        states = batch_store.step_states(job.id)
        body = receipt_body(
            job.spec, keys, states, segment_start=job.segment_start_ts,
            eta_window=job.settings.eta_window, eta_min_samples=job.settings.eta_min_samples,
        )
        return {
            "job_id": job.id, "name": job.name, "status": job.status.value,
            "pause_reason": job.pause_reason or None, "order": job.settings.order, "items": len(keys),
            "stage_specs": [{"name": s.name, "model": s.model, "depends_on": list(s.depends_on)}
                            for s in job.spec.stages],
            "created_ts": job.created_ts, "updated_ts": job.updated_ts,
            "started_ts": job.started_ts, "finished_ts": job.finished_ts,
            "cancel_requested": job.cancel_requested, **body,
        }

    # -- tool bodies --------------------------------------------------------
    def create(self, name: str, stages: list[dict[str, Any]], fields: list[str] | None,
               items: list[dict[str, Any]] | None, start: bool, token: str) -> dict[str, Any]:
        config = self._enabled()
        _plain_bool(start, "start")
        limits = _limits(config)
        if items and not fields:
            first = items[0] if isinstance(items, list) and isinstance(items[0], dict) else {}
            inferred = first.get("fields")
            fields = sorted(inferred) if isinstance(inferred, dict) else []
        rt = self._rt()
        spec = validate_spec({"name": name, "fields": fields or [], "stages": stages}, limits,
                             is_cloud_model=rt._is_cloud_model_name)
        spec = self._resolve_models(spec)
        validated = validate_items(items or [], spec, limits)
        owner, role = self._owner(token)
        job = batch_store.create_job(
            name=spec.name, request_owner=owner, request_role=role, spec=spec,
            settings=_settings(config), ttl_seconds=config.retention_ttl_seconds,
        )
        try:
            if validated:
                batch_store.add_items(job.id, self._seal_items(validated), max_items=config.max_items_per_job)
            if start:
                job = batch_store.start_job(job.id, max_active=config.max_active_jobs)
        except Exception as exc:
            raise self._abandon_draft(job.id, exc) from exc
        if start:
            self.worker.submit(job.id)
        return {"ok": True, "job": self._receipt(self._current(job.id))}

    def add_items(self, job_id: str, items: list[dict[str, Any]], token: str) -> dict[str, Any]:
        config = self._enabled()
        job = self._authorized(job_id, token)
        validated = validate_items(items, job.spec, _limits(config), existing_keys=batch_store.item_keys(job.id))
        total = batch_store.add_items(job.id, self._seal_items(validated), max_items=config.max_items_per_job)
        return {"ok": True, "job_id": job.id, "added": len(validated), "items": total}

    def start(self, job_id: str, token: str) -> dict[str, Any]:
        config = self._enabled()
        job = self._authorized(job_id, token)
        job = batch_store.start_job(job.id, max_active=config.max_active_jobs)
        self.worker.submit(job.id)
        return {"ok": True, "job": self._receipt(job)}

    def status(self, job_id: str, token: str) -> dict[str, Any]:
        return {"ok": True, "job": self._receipt(self._authorized(job_id, token))}

    def results(self, job_id: str, stage: str, offset: int, limit: int, include_answers: bool,
                token: str) -> dict[str, Any]:
        _plain_int(offset, "offset", 0, 10_000_000)
        _plain_int(limit, "limit", 1, MAX_RESULTS_PAGE)
        _plain_bool(include_answers, "include_answers")
        job = self._authorized(job_id, token)
        names = [spec_stage.name for spec_stage in job.spec.stages]
        if stage and stage not in names:
            raise InvalidInput("unknown stage: %s" % stage)
        keys = batch_store.item_keys(job.id)
        states = batch_store.step_states(job.id)
        effective = effective_statuses(job.spec, keys, states)
        executor = self.executor()
        page = keys[offset:offset + limit]
        rows: list[dict[str, Any]] = []
        for key in page:
            steps: list[dict[str, Any]] = []
            owned: list[StepState] = []
            for name in names:
                if stage and name != stage:
                    continue
                state = states.get((name, key)) or StepState(name, key)
                entry = step_entry(state, effective[(name, key)])
                if include_answers and state.status is StepStatus.ANSWERED:
                    entry["answer"], entry["answer_truncated"] = executor.read_answer(state)
                steps.append(entry)
                owned.append(state)
            rows.append({"item_key": key, "steps": steps, **token_rollup(owned)})
        end = offset + len(page)
        return {"ok": True, "job_id": job.id, "stage": stage or None, "offset": offset,
                "total_items": len(keys), "next_offset": end if end < len(keys) else None, "items": rows}

    def recent(self, limit: int, include_finished: bool, token: str) -> dict[str, Any]:
        _plain_int(limit, "limit", 1, 100)
        _plain_bool(include_finished, "include_finished")
        rt = self._rt()
        owner, account = rt._direct_fanout_identity(token)
        scoped = rt._deployment_authenticates_callers() and str((account or {}).get("role") or "") != "admin"
        return {"ok": True, "jobs": batch_store.recent_jobs(
            request_owner=owner if scoped else None, include_finished=include_finished, limit=limit)}

    def cancel(self, job_id: str, token: str) -> dict[str, Any]:
        job = self._authorized(job_id, token)
        job = batch_store.request_cancel(job.id)
        # The in-flight step keeps the truthful skipped/unknown fanout semantics.
        for run_id in batch_store.active_child_runs(job.id):
            fanout_store.request_cancel(run_id)
        return {"ok": True, "job": self._receipt(self._current(job.id))}

    def resume(self, job_id: str, include_failed: bool, token: str) -> dict[str, Any]:
        config = self._enabled()
        _plain_bool(include_failed, "include_failed")
        job = self._authorized(job_id, token)
        job = batch_store.resume_job(job.id, include_failed=include_failed, max_active=config.max_active_jobs)
        self.worker.submit(job.id)
        return {"ok": True, "job": self._receipt(job)}


def register(mcp: _McpServer, record: Callable[..., object] | None,
             runtime: Callable[[], ToolsRuntime] | None = None) -> BatchJobService:
    """Register the ``batch_job_*`` tools; ``record`` is the server's ``_record_direct_tool``.

    The tools' schemas use the host's strict integer and boolean types so a
    ``True`` or ``"5"`` is refused at the MCP boundary like ``model_fanout_*``;
    this package may not import the validation library itself, so they come
    from the runtime object (plain ``int``/``bool`` when there is none, with
    the same checks repeated inside each tool body).
    """
    service = BatchJobService(runtime, record)
    host = runtime() if callable(runtime) else None
    if TYPE_CHECKING:
        # The host's validation types are chosen at runtime; the checker sees plain int and bool.
        StrictInt: TypeAlias = int  # noqa: N806
        StrictBool: TypeAlias = bool  # noqa: N806
    else:
        StrictInt = getattr(host, "StrictInt", int)  # noqa: N806 - used as an annotation type
        StrictBool = getattr(host, "StrictBool", bool)  # noqa: N806

    def run(name: str, token: str, shown: dict[str, Any], body: Callable[[], dict[str, Any]]) -> str:
        started = time.time()
        try:
            service._gate(name, token, started)
            payload = body()
        except SonderError as exc:
            payload = error_payload(exc)
        except Exception:  # noqa: BLE001 - a tool reports a typed failure, it never crashes the server
            logger.exception("%s failed", name)
            payload = error_payload(InternalFailure("batch job tool failed"))
        output = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        if callable(record):
            record(name, shown, ok=bool(payload.get("ok")), started=started,
                   summary=("ok" if payload.get("ok") else payload["error"]["code"]), output=output[:2000])
        return output

    @mcp.tool()
    def batch_job_create(name: str, stages: list[dict[str, Any]], fields: list[str] | None = None,
                         items: list[dict[str, Any]] | None = None, start: StrictBool = False,
                         token: str = "") -> str:
        """Create a durable batch job: N items through one or more local-model stages.

        ``stages`` is a list of {name, model, prompt, system, num_predict, num_ctx, think,
        temperature, timeout_s, depends_on}; a prompt may use {{field}} and, for a later
        stage, {{stage.<earlier stage>}}.  ``items`` are {key, fields:{...}}.  Local models
        only.  With ``start`` true the job is queued at once; otherwise call batch_job_start.
        """
        shown = {"name": name, "stages": len(stages or []), "items": len(items or []), "start": start}
        return run("batch_job_create", token, shown,
                   lambda: service.create(name, stages, fields, items, start, token))

    @mcp.tool()
    def batch_job_add_items(job_id: str, items: list[dict[str, Any]], token: str = "") -> str:
        """Add a page of {key, fields} items to a draft, queued, running or paused job."""
        return run("batch_job_add_items", token, {"job_id": job_id, "items": len(items or [])},
                   lambda: service.add_items(job_id, items, token))

    @mcp.tool()
    def batch_job_start(job_id: str, token: str = "") -> str:
        """Queue a draft job; one worker runs queued jobs in order."""
        return run("batch_job_start", token, {"job_id": job_id}, lambda: service.start(job_id, token))

    @mcp.tool()
    def batch_job_status(job_id: str, token: str = "") -> str:
        """Progress (done/total, rate, ETA), token totals, served models, failures; no answers."""
        return run("batch_job_status", token, {"job_id": job_id}, lambda: service.status(job_id, token))

    @mcp.tool()
    def batch_job_results(job_id: str, stage: str = "", offset: StrictInt = 0, limit: StrictInt = 50,
                          include_answers: StrictBool = True, token: str = "") -> str:
        """One page of per-item results with each step's exact answer, models and tokens (owner only)."""
        return run("batch_job_results", token, {"job_id": job_id, "stage": stage, "offset": offset, "limit": limit},
                   lambda: service.results(job_id, stage, offset, limit, include_answers, token))

    @mcp.tool()
    def batch_job_recent(limit: StrictInt = 20, include_finished: StrictBool = True, token: str = "") -> str:
        """List recent batch jobs available to this caller (ids, states and counts only)."""
        return run("batch_job_recent", token, {"limit": limit}, lambda: service.recent(limit, include_finished, token))

    @mcp.tool()
    def batch_job_cancel(job_id: str, token: str = "") -> str:
        """Stop a job: no further step is dispatched; an in-flight call keeps the truthful fanout state."""
        return run("batch_job_cancel", token, {"job_id": job_id}, lambda: service.cancel(job_id, token))

    @mcp.tool()
    def batch_job_resume(job_id: str, include_failed: StrictBool = False, token: str = "") -> str:
        """Resume a paused, interrupted or cancelled job; an answered step is never run again."""
        return run("batch_job_resume", token, {"job_id": job_id, "include_failed": include_failed},
                   lambda: service.resume(job_id, include_failed, token))

    return service


__all__ = ["BatchJobService", "BatchWorker", "error_payload", "register"]
