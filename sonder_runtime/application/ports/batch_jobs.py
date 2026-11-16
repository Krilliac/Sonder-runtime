"""Ports for the batch-job runner: durable job state and single-step execution.

The runner depends only on these protocols.  The persistence adapter
(``adapters/persistence/batch_store``) and the bootstrap step executor (a
single-model durable fanout child run) implement them; tests substitute fakes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from sonder_runtime.domain.batch_jobs import (
    ItemSpec, JobSpec, JobStatus, PauseReason, StageSpec, StepReason, StepState,
)


@dataclass(frozen=True)
class RunSettings:
    """Per-job knobs frozen at creation from ``[batch_jobs]``, plus the resume choice."""

    order: str = "stage_major"
    max_consecutive_failures: int = 5
    eta_window: int = 20
    eta_min_samples: int = 3
    max_rendered_prompt_chars: int = 32000
    answer_max_chars: int = 64000
    max_item_timeout_seconds: int = 900
    # Set by an explicit resume: failed steps run again in this segment only.
    retry_failed: bool = False


@dataclass(frozen=True)
class JobRecord:
    id: str
    name: str
    status: JobStatus
    pause_reason: str
    spec: JobSpec
    settings: RunSettings
    request_owner: str
    request_role: str
    segment_start_ts: float | None
    created_ts: float
    updated_ts: float
    started_ts: float | None = None
    finished_ts: float | None = None
    cancel_requested: bool = False


@dataclass(frozen=True)
class JobOutcome:
    status: JobStatus
    pause_reason: PauseReason | None = None
    steps_run: int = 0


@dataclass(frozen=True)
class StepRequest:
    """One dispatch: a rendered step plus the prior state to resume, if any."""

    job: JobRecord
    stage: StageSpec
    item_key: str
    system: str
    prompt: str
    state: StepState | None = None


class BatchStorePort(Protocol):
    def claim_job(self, job_id: str, owner_id: str, owner_pid: int, lease_seconds: int) -> JobRecord | None: ...
    def heartbeat(self, job_id: str, owner_id: str, lease_seconds: int) -> bool: ...
    def cancel_requested(self, job_id: str) -> bool: ...
    def item_keys(self, job_id: str) -> Sequence[str]: ...
    def step_states(self, job_id: str) -> Mapping[tuple[str, str], StepState]: ...
    def finish(self, job_id: str, owner_id: str, status: JobStatus, pause_reason: PauseReason | None) -> bool: ...


class StepExecutorPort(Protocol):
    def item(self, job_id: str, item_key: str) -> ItemSpec: ...
    def read_answer(self, state: StepState) -> tuple[str, bool]: ...
    def execute(self, request: StepRequest) -> StepState: ...
    def fail(self, request: StepRequest, reason: StepReason) -> StepState: ...
