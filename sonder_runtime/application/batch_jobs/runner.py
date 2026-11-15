"""The batch-job loop: pick the next runnable step, render, execute, account, repeat.

``run_job`` runs one claimed job to a stopping point -- completed, cancelled,
paused by the circuit breaker, or interrupted when its lease is lost -- and
returns a :class:`JobOutcome`.  Persistence and model execution are ports, so
the loop is exercised with in-memory fakes; it never touches a clock, a
database or a provider itself.

Resume discipline: steps whose last result was ``unknown`` or ``skipped`` (an
interrupted or cancelled attempt), and ``failed`` ones only when the job was
resumed with ``retry_failed``, are treated as pending *once* in this segment.
A step answered earlier is never dispatched again.
"""
from __future__ import annotations

import logging
from dataclasses import replace

from sonder_runtime.application.ports.batch_jobs import (
    BatchStorePort, JobOutcome, JobRecord, StepExecutorPort, StepRequest,
)
from sonder_runtime.domain.batch_jobs import (
    BREAKER_FAILURE_CLASSES, JobStatus, PauseReason, StepReason, StepState, StepStatus,
    breaker_trips, effective_statuses, is_finished, next_step, referenced_stages, render_prompt,
)
from sonder_runtime.domain.common.errors import SonderError

logger = logging.getLogger(__name__)

# The lease covers one step's longest allowed call plus headroom for the
# receipt write; a heartbeat after every step renews it.
LEASE_MARGIN_SECONDS = 120
MAX_LEASE_SECONDS = 3600


def lease_seconds_for(job: JobRecord) -> int:
    longest = max((stage.timeout_s for stage in job.spec.stages), default=0)
    return min(MAX_LEASE_SECONDS, longest + LEASE_MARGIN_SECONDS)


def _retry_set(states, settings) -> set[tuple[str, str]]:
    retryable = {StepStatus.UNKNOWN, StepStatus.SKIPPED}
    if settings.retry_failed:
        retryable.add(StepStatus.FAILED)
    return {key for key, state in states.items() if state.status in retryable}


def _view(states, retry) -> dict[tuple[str, str], StepState]:
    """States as the scheduler sees them: a step due for retry looks pending."""
    return {
        key: (replace(state, status=StepStatus.PENDING) if key in retry else state)
        for key, state in states.items()
    }


def _prepare(job, executor, stage, key, states):
    """Render one step.  Returns ``(request, None)`` or ``(request, reason)`` when it cannot run."""
    item = executor.item(job.id, key)
    request = StepRequest(job=job, stage=stage, item_key=key, system="", prompt="", state=states.get((stage.name, key)))
    upstream = {}
    for name in referenced_stages(stage):
        source = states.get((name, key))
        if source is None or source.status is not StepStatus.ANSWERED:
            return request, StepReason.UPSTREAM_NOT_ANSWERED
        try:
            text, truncated = executor.read_answer(source)
        except SonderError:
            return request, StepReason.UPSTREAM_NOT_ANSWERED
        if truncated:
            return request, StepReason.UPSTREAM_TRUNCATED
        upstream[name] = text
    system, prompt = render_prompt(stage, item, upstream)
    if len(system) + len(prompt) > job.settings.max_rendered_prompt_chars:
        return request, StepReason.PROMPT_TOO_LARGE
    return replace(request, system=system, prompt=prompt), None


def run_job(job_id: str, *, store: BatchStorePort, executor: StepExecutorPort,
            owner_id: str, owner_pid: int) -> JobOutcome:
    """Run a queued job until it completes, is cancelled, pauses or loses its lease."""
    job = store.claim_job(job_id, owner_id, owner_pid, MAX_LEASE_SECONDS)
    if job is None:
        return JobOutcome(status=JobStatus.INTERRUPTED)
    lease = lease_seconds_for(job)
    spec, settings = job.spec, job.settings
    states = dict(store.step_states(job_id))
    retry = _retry_set(states, settings)
    consecutive = steps_run = 0

    def stop(status, reason=None):
        store.finish(job_id, owner_id, status, reason)
        return JobOutcome(status=status, pause_reason=reason, steps_run=steps_run)

    while True:
        if store.cancel_requested(job_id):
            return stop(JobStatus.CANCELLED)
        keys = list(store.item_keys(job_id))
        effective = effective_statuses(spec, keys, _view(states, retry))
        step = next_step(spec, keys, effective, order=settings.order)
        if step is None:
            return stop(JobStatus.COMPLETED if is_finished(effective) else JobStatus.INTERRUPTED)
        stage_name, key = step
        stage = spec.stage(stage_name)
        request, reason = _prepare(job, executor, stage, key, states)
        if reason is not None:
            result = executor.fail(request, reason)
        else:
            result = executor.execute(request)
        if result.status in (StepStatus.PENDING, StepStatus.RUNNING):
            # A step the executor could not bring to a final state would be picked again at once, forever.
            logger.warning("batch job %s stopped: step %s/%s ended non-final (%s)", job_id, stage_name, key, result.status.value)
            return stop(JobStatus.INTERRUPTED)
        states[(stage_name, key)] = result
        retry.discard((stage_name, key))
        steps_run += 1
        if result.status is StepStatus.FAILED and result.failure_class in BREAKER_FAILURE_CLASSES:
            consecutive += 1
        else:
            consecutive = 0
        if breaker_trips(consecutive, settings.max_consecutive_failures):
            logger.warning("batch job %s paused after %d consecutive backend failures", job_id, consecutive)
            return stop(JobStatus.PAUSED, PauseReason.CONSECUTIVE_FAILURES)
        if not store.heartbeat(job_id, owner_id, lease):
            # Another worker owns the job now; do not write a status over it.
            return JobOutcome(status=JobStatus.INTERRUPTED, steps_run=steps_run)
