"""End-to-end wiring: compute-cancel attempt identities on the production path.

``ComputeJobWorker.cancel`` derives a per-attempt journal identity from the
durable worker-effects journal (#515).  This drives it through the real
entry point: the bootstrap-composed compute worker (``build_application``)
reached through the HTTP facade the serve route uses
(``dispatch_compute_job_cancel``).

Only the host OS containment seam is replaced: this container has no live
systemd manager, so the scoped memory limiter, launcher and process identity
are the same doubles the production-composition capacity test uses.  The
limiter's first quiesce reports cleanup still pending, so the first cancel
settles as ``cancellation_requested`` and the retry is a genuine second
attempt.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def _records(journal, job_id: str):
    page = journal.effects_since("runtime:compute-jobs", 0, limit=1000)
    return {
        record.operation_id: record for record in page.records
        if record.operation_id.startswith("compute-cancel:")
        and record.operation_id.rsplit(":", 1)[-1] == job_id
    }


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process job")
def test_http_cancel_uses_journal_derived_attempt_identity(tmp_path, monkeypatch):
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from sonder_runtime.adapters.extensions.memory_limits import ProcessContainmentResult
    from sonder_runtime.bootstrap import app as bootstrap
    from tests.test_job004_process_provider import _Process, _ScopedLimiter, _ScopedToken
    from sonder_runtime.application.compute_fabric.jobs import (
        ComputeJobWorker,
        RemoteJobEnvelope,
    )
    from sonder_runtime.application.execution.effect_journal import (
        EffectState,
    )
    from sonder_runtime.domain.compute_fabric import WorkloadKind
    from sonder_runtime.interfaces.http.facades.compute_fabric import (
        dispatch_compute_job_cancel,
    )
    from sonder_runtime.platform.config import (
        ComputeConfig,
        ComputeJobConfig,
        SonderConfig,
        StateConfig,
    )

    monkeypatch.setattr(
        ComputeJobWorker, "_artifact_stage_base",
        staticmethod(lambda: tmp_path / "artifact-stages"),
    )
    class _CancelScriptedToken(_ScopedToken):
        """Scripted results apply to forced (cancellation) quiesces only."""

        def quiesce(self, *, force: bool) -> ProcessContainmentResult:
            self.calls.append(force)
            if force and self.results:
                return self.results.pop(0)
            return ProcessContainmentResult(True)

    class _Running(_Process):
        """A launched job that keeps running until the provider kills it."""

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if not self.killed:
                raise subprocess.TimeoutExpired("sleeper", timeout or 0)
            return -9

    token = _CancelScriptedToken()
    monkeypatch.setattr(bootstrap, "SubprocessJobProvider", lambda registry, **kwargs:
        SubprocessJobProvider(registry, **kwargs, memory_limiter=_ScopedLimiter(token),
                              launcher=lambda *a, **kw: _Running(),
                              process_identity_resolver=lambda pid: "wiring-process"))
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
    application = bootstrap.build_application(config=config)
    worker = application.compute_job_worker()
    journal = worker._effect_binding.journal
    submitted = worker.submit(RemoteJobEnvelope.create(
        controller_job_id="wiring-cancel", idempotency_key="wiring-cancel",
        workload=WorkloadKind.TEST, catalog_entry_id="sleeper",
        workspace_mapping=tmp_path.name,
    ))
    job = submitted.remote_job_id
    # From here the containment unit reports one pending quiesce; every
    # later quiesce finds it empty.
    token.results[:] = [ProcessContainmentResult(False, detail="descendant remains")]
    assert worker.status(job).state in {"pending", "running"}
    first = dispatch_compute_job_cancel(worker, job, "operator cancel")
    assert first.body["job"]["state"] == "cancellation_requested", first.body
    records = _records(journal, job)
    # Attempt 1 keeps the historical identity and settles as a receipt.
    attempt_one = f"compute-cancel:{worker.worker_id}:{job}"
    assert set(records) == {attempt_one}
    assert records[attempt_one].state is EffectState.COMPLETED
    assert records[attempt_one].receipt_key == f"{job}:cancellation_requested"

    # The retry is admitted as attempt 2, derived from the durable journal,
    # with its own operation id and idempotency key.
    second = dispatch_compute_job_cancel(worker, job, "operator retry")
    second_state = second.body["job"]["state"]
    records = _records(journal, job)
    attempt_two = f"compute-cancel:{worker.worker_id}:attempt-2:{job}"
    assert set(records) == {attempt_one, attempt_two}
    assert records[attempt_two].state is EffectState.COMPLETED
    assert records[attempt_two].idempotency_key == f"cancel:{job}:attempt-2"
    assert records[attempt_two].receipt_key == f"{job}:{second_state}"
    # Attempt 1 is untouched by the retry.
    assert records[attempt_one].receipt_key == f"{job}:cancellation_requested"
