"""Startup reconciliation proves interrupted compute cancels only from registry proof (#515).

Drives the production composition (``build_application``): the bootstrap
compute worker cancels a job and loses the receipt after the provider acted
(an uncertain intent also blocks every later effect of that composition, so
each case has one).  The next composition's startup pass, through the
bootstrap-registered ``compute-cancel`` verifier, must:

* prove a cancellation whose cleanup completed from the durable registry
  (``verified:durable-compute-cancel-v1``) and clear the compute run's fence;
* leave a cancellation whose cleanup is still pending ``uncertain`` and the
  run fenced, so no compute worker can be composed, and clear the fence only
  after the provider's own cleanup retry makes the terminal cancelled,
  cleaned record durable and an operator pass proves it.

Only the host OS containment seam (no live systemd manager here), the
process launcher and the deadline timer are replaced.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest


class _ManualTimer:
    armed: list = []

    def __init__(self, interval, function, args=()):
        self.function, self.args, self.daemon = function, args, True

    def start(self):
        _ManualTimer.armed.append(self)

    def cancel(self):
        return None


def _fence(database) -> tuple[int, ...]:
    with sqlite3.connect(database) as connection:
        return tuple(row[0] for row in connection.execute(
            "SELECT recovery_required FROM effect_owner WHERE run_id='runtime:compute-jobs'"
        ).fetchall())


def _first_composition(tmp_path, monkeypatch, *, pending: bool):
    """Compose, submit one job, cancel it and lose the receipt."""
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from sonder_runtime.adapters.extensions.memory_limits import (
        PreparedProcessContainment,
        ProcessContainmentResult,
    )
    from sonder_runtime.application.compute_fabric.jobs import (
        ComputeJobWorker,
        RemoteJobEnvelope,
    )
    from sonder_runtime.bootstrap import app as bootstrap
    from sonder_runtime.domain.compute_fabric import WorkloadKind
    from sonder_runtime.platform.config import (
        ComputeConfig,
        ComputeJobConfig,
        SonderConfig,
        StateConfig,
    )
    from tests.test_job004_process_provider import _Process, _ScopedToken

    monkeypatch.setattr(
        ComputeJobWorker, "_artifact_stage_base",
        staticmethod(lambda: tmp_path / "artifact-stages"),
    )

    class _Scope(_ScopedToken):
        def __init__(self) -> None:
            super().__init__()
            self.emptied = False

        def quiesce(self, *, force: bool) -> ProcessContainmentResult:
            self.calls.append(force)
            if force and self.results:
                return self.results.pop(0)
            if force:
                self.emptied = True
            return ProcessContainmentResult(True)

    scopes: dict[str, _Scope] = {}

    class _Limiter:
        """One containment scope per job, as systemd scopes are."""

        def prepare_process_job(self, job_id, argv, memory_limit_bytes, process_limit):
            scopes.setdefault(job_id, _Scope())
            return PreparedProcessContainment(
                argv=tuple(argv), launch_options={"scope_marker": job_id},
                token=scopes[job_id],
            )

        def apply(self, process, limit_bytes):
            raise AssertionError("scoped process jobs must not use the fallback limiter")

        def restore_process_job(self, job_id, metadata):
            return scopes[job_id]

    class _Running(_Process):
        def __init__(self, scope: _Scope) -> None:
            super().__init__(-9)
            self.scope = scope

        def wait(self, timeout=None):
            if not self.scope.emptied:
                raise subprocess.TimeoutExpired("sleeper", timeout or 0)
            return -9

        def poll(self):
            return -9 if self.scope.emptied else None

    providers = []

    def provider_factory(registry, **kwargs):
        provider = SubprocessJobProvider(
            registry, **kwargs, memory_limiter=_Limiter(),
            launcher=lambda argv, **kw: _Running(scopes[kw["scope_marker"]]),
            process_identity_resolver=lambda pid: "wiring-process",
            timer_factory=_ManualTimer,
        )
        providers.append(provider)
        return provider

    monkeypatch.setattr(bootstrap, "SubprocessJobProvider", provider_factory)
    _ManualTimer.armed = []
    config = SonderConfig(
        state=StateConfig(home=str(tmp_path), workspace_roots=(str(tmp_path),)),
        compute=ComputeConfig(
            worker_memory_budget_bytes=1 << 34,
            jobs=(ComputeJobConfig(
                job_id="sleeper", workload="test", program=sys.executable,
                fixed_args=("-c", "import time; time.sleep(60)"),
                workspace_mappings=(tmp_path.name,),
            ),),
        ),
    )
    first = bootstrap.build_application(config=config)
    worker = first.compute_job_worker()
    provider = providers[-1]
    job = worker.submit(RemoteJobEnvelope.create(
        controller_job_id="wiring-cancel", idempotency_key="wiring-cancel",
        workload=WorkloadKind.TEST, catalog_entry_id="sleeper",
        workspace_mapping=tmp_path.name,
    )).remote_job_id
    if pending:
        scopes[job].results[:] = [
            ProcessContainmentResult(False, detail="descendant remains"),
        ]
    original_cancel = provider.cancel

    def cancel_then_lose_receipt(job_id, reason="cancelled"):
        original_cancel(job_id, reason)
        raise RuntimeError("receipt publication lost")

    monkeypatch.setattr(provider, "cancel", cancel_then_lose_receipt)
    with pytest.raises(RuntimeError, match="receipt publication lost"):
        worker.cancel(job, "operator cancel")
    intent_id = f"runtime:compute-jobs:compute-cancel:{worker.worker_id}:{job}"
    return bootstrap, config, first, job, intent_id, provider


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process job")
def test_startup_pass_proves_a_cleaned_cancel_and_clears_the_fence(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
    from sonder_runtime.application.execution.effect_journal import (
        EffectJournalError,
        EffectState,
    )
    from sonder_runtime.application.ports.jobs import JobStatus

    bootstrap, config, first, job, intent_id, orphan = _first_composition(
        tmp_path, monkeypatch, pending=False,
    )
    assert first.job_registry().poll(job).status is JobStatus.CANCELLED
    database = tmp_path / "worker-effects.db"
    journal = SQLiteEffectJournal(database)
    assert journal.get(intent_id).state is EffectState.UNCERTAIN

    # A new composition (newer host epoch) runs the bounded startup pass.
    second = bootstrap.build_application(config=config)
    settled = journal.get(intent_id)
    assert settled.state is EffectState.COMPLETED
    assert settled.receipt_key == f"{job}:cancelled"
    assert settled.detail.startswith("verified:durable-compute-cancel-v1:job-registry:")
    assert set(_fence(database)) == {0}
    restarted = second.compute_job_worker()
    with pytest.raises(EffectJournalError, match="already completed"):
        restarted.cancel(job, "operator retry")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process job")
def test_startup_pass_keeps_a_pending_cancel_fenced_until_cleanup_is_durable(
    tmp_path, monkeypatch,
):
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
    from sonder_runtime.application.execution.effect_journal import (
        EffectJournalError,
        EffectState,
    )
    from sonder_runtime.application.ports.jobs import JobStatus

    bootstrap, config, first, job, intent_id, orphan = _first_composition(
        tmp_path, monkeypatch, pending=True,
    )
    registry = first.job_registry()
    assert registry.poll(job).status is JobStatus.CANCELLATION_REQUESTED
    database = tmp_path / "worker-effects.db"
    journal = SQLiteEffectJournal(database)

    second = bootstrap.build_application(config=config)
    assert journal.get(intent_id).state is EffectState.UNCERTAIN
    assert 1 in _fence(database)
    with pytest.raises(EffectJournalError, match="reconciliation"):
        second.compute_job_worker()
    again = second.worker_effect_reconciliation()
    assert intent_id in [item.intent_id for item in again.fenced]
    assert [item.intent_id for item in again.resolved] == []
    assert 1 in _fence(database)

    # The orphaned provider's own cleanup retry completes containment; the
    # durable record becomes terminal cancelled with cleanup evidence.
    # (The new composition's provider armed its own deadline for the job; it
    # does not own the process and is deliberately left unfired here.)
    retries = [
        timer for timer in _ManualTimer.armed
        if timer.args == (job,) and getattr(timer.function, "__self__", None) is orphan
    ]
    assert retries
    retries[-1].function(*retries[-1].args)
    assert registry.poll(job).status is JobStatus.CANCELLED
    assert registry.process_cleanup_proof(job) is not None

    proven = second.worker_effect_reconciliation()
    assert [item.intent_id for item in proven.resolved] == [intent_id]
    assert journal.get(intent_id).state is EffectState.COMPLETED
    assert set(_fence(database)) == {0}
    restarted = second.compute_job_worker()
    with pytest.raises(EffectJournalError, match="already completed"):
        restarted.cancel(job, "operator retry")
