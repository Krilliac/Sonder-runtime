"""Runs one batch step as a single-model durable fanout child run.

The child run is executed by the server's existing ``_execute_fanout_run``, so
the lease, the pre-transport dispatch fence, cancel truthfulness, model health
and restart recovery are the ones plain fanout already has.  This adapter only
creates (or resumes) the child, seals its system+prompt, and reads the single
result row back as a :class:`StepState`.  Answers are sealed by the executor
itself (``sealed_answers``), so this module never handles plaintext at rest.

``runtime`` is the server module (or a test double) providing
``_execute_fanout_run`` and ``_fanout_worker_id`` and the ``fanout_prompt_vault``
module; it is passed in so this module never imports ``server``.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, Protocol

from sonder_runtime.adapters import fanout_execution
from sonder_runtime.adapters.persistence import batch_store, fanout_store
from sonder_runtime.application.ports.batch_jobs import StepRequest
from sonder_runtime.domain.batch_jobs import ItemSpec, StepReason, StepState, StepStatus
from sonder_runtime.domain.common.errors import IntegrityFailure

_MARKER = "sealed-fanout-prompt:batch-step"
_RETRYABLE = (StepStatus.UNKNOWN, StepStatus.SKIPPED, StepStatus.FAILED)


class PromptVault(Protocol):
    """The ``fanout_prompt_vault`` module surface this adapter uses."""

    PromptVaultError: type[Exception]

    def encrypt_prompt(self, prompt: str) -> str: ...

    def decrypt_prompt(self, ciphertext: str) -> str: ...


class ExecutorRuntime(Protocol):
    """The server-module surface the executor uses (the server module or a test double)."""

    @property
    def fanout_prompt_vault(self) -> PromptVault: ...

    def _execute_fanout_run(self, run_id: str) -> object: ...

    def _fanout_worker_id(self) -> str: ...


class FanoutStepExecutor:
    """``StepExecutorPort`` over durable fanout child runs."""

    def __init__(self, runtime: ExecutorRuntime) -> None:
        self._runtime = runtime

    @property
    def _vault(self) -> PromptVault:
        return self._runtime.fanout_prompt_vault

    def _open(self, ciphertext: str) -> str:
        try:
            return self._vault.decrypt_prompt(ciphertext or "")
        except self._vault.PromptVaultError as exc:
            raise IntegrityFailure("sealed batch data is unavailable") from exc

    def seal_fields(self, fields: Mapping[str, str]) -> str:
        try:
            return self._vault.encrypt_prompt(json.dumps(dict(fields), ensure_ascii=False, sort_keys=True))
        except self._vault.PromptVaultError as exc:
            raise IntegrityFailure("could not seal batch data") from exc

    def item(self, job_id: str, item_key: str) -> ItemSpec:
        ciphertext = batch_store.item_ciphertext(job_id, item_key)
        if ciphertext is None:
            raise IntegrityFailure("batch item is missing")
        return ItemSpec(item_key, json.loads(self._open(ciphertext)))

    def read_answer(self, state: StepState) -> tuple[str, bool]:
        """The exact sealed answer text and whether it was cut at ``answer_max_chars``."""
        ciphertext = fanout_store.sealed_answer_ciphertext(state.run_id, state.requested_model)
        if not ciphertext:
            raise IntegrityFailure("sealed answer is missing")
        return self._open(ciphertext), state.answer_truncated or not state.answer_truncation_known

    def _limits(self, request: StepRequest) -> dict[str, Any]:
        stage, settings = request.stage, request.job.settings
        return {
            "num_predict": stage.num_predict, "timeout": stage.timeout_s,
            "timeout_cap": settings.max_item_timeout_seconds, "cloud_workers": 1,
            # No residency snapshot: a batch never unloads a model after a step.
            "resident_before": [], "resident_snapshot_known": False, "plan_skipped": [],
            "selection_profile": "", "temperature": stage.temperature, "num_ctx": stage.num_ctx,
            "think": stage.think, "sealed_answers": True, "answer_max_chars": settings.answer_max_chars,
            "batch_job_id": request.job.id,
        }

    def _seal(self, system: str, prompt: str) -> str:
        try:
            return self._vault.encrypt_prompt(fanout_execution.seal_envelope(system, prompt))
        except self._vault.PromptVaultError as exc:
            raise IntegrityFailure("could not seal batch data") from exc

    def _create(self, request: StepRequest, system: str, prompt: str) -> str:
        sealed = self._seal(system, prompt)
        run = fanout_store.create_run(
            _MARKER, [request.stage.model], request_owner=request.job.request_owner,
            request_role=request.job.request_role, scope="local", cloud_opt_in=False,
            limits=self._limits(request), execution_prompt_ciphertext=sealed,
            batch_job_id=request.job.id, batch_item_key=request.item_key, batch_stage=request.stage.name,
        )
        run_id: str = run["id"]
        return run_id

    def _ready_run(self, request: StepRequest, system: str, prompt: str) -> str:
        """A queued child run for this step: resumed when one exists, else created."""
        prior = request.state
        if prior is None or not prior.run_id:
            return self._create(request, system, prompt)
        if prior.status in _RETRYABLE:
            if prompt:
                # The prior attempt may have failed before any model call (sealed empty); carry the fresh render.
                fanout_store.replace_execution_prompt_ciphertext(prior.run_id, self._seal(system, prompt))
            # Local rows only, so an unknown attempt is replayed without a metered-cost risk.
            resumed = fanout_store.resume_run(
                prior.run_id, include_failed=prior.status is StepStatus.FAILED, retry_unknown=True,
            )
            if resumed is None:
                raise IntegrityFailure("batch step could not be resumed")
        else:
            # A finished child run whose single row never left 'pending' (the worker died between the
            # run claim and the row claim) reads as PENDING; resume_run requeues it, a live run is left alone.
            run = fanout_store.get_run(prior.run_id)
            if run is not None and run["status"] in fanout_store.TERMINAL_RUNS:
                if fanout_store.resume_run(prior.run_id) is None:
                    raise IntegrityFailure("batch step could not be resumed")
        return prior.run_id

    def _state(self, run_id: str) -> StepState:
        state = batch_store.step_state_for_run(run_id)
        if state is None:
            raise IntegrityFailure("batch step result is missing")
        return state

    def execute(self, request: StepRequest) -> StepState:
        try:
            run_id = self._ready_run(request, request.system, request.prompt)
        except ValueError:
            # The only limit create_run can trip here is the sealed-prompt size.
            return self.fail(request, StepReason.PROMPT_TOO_LARGE)
        self._runtime._execute_fanout_run(run_id)
        return self._state(run_id)

    def fail(self, request: StepRequest, reason: StepReason) -> StepState:
        """Record a failed step that never reached a model, with a closed reason."""
        run_id = self._ready_run(request, "", "")
        owner = self._runtime._fanout_worker_id()
        fanout_store.claim_run(run_id, owner, owner_pid=os.getpid())
        row = fanout_store.claim_next_result(run_id, owner, owner_pid=os.getpid())
        if row is not None:
            fanout_store.record_result(
                run_id, row["model"], owner, "failed", error=reason.value, elapsed_ms=0,
                failure_class="configuration",
            )
        return self._state(run_id)
