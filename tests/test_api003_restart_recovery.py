"""API-003 restart rehearsal over the durable process-job composition."""
from __future__ import annotations

import os
import sys

from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.execution.process_jobs import ProcessJobRequest
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus


def test_provider_job_reopens_and_reconciles_after_owner_restart(tmp_path):
    database = tmp_path / "api003-restart.db"
    cleanup = ProcessTreeSupervisor(platform_name=os.name, timeout_seconds=5)
    first_registry = SQLiteDurableJobRegistry(database)
    provider = SubprocessJobProvider(
        first_registry,
        process_cleanup=cleanup,
        platform_name=os.name,
    )
    job_id = "api003-restart"
    try:
        provider.start(ProcessJobRequest(
            JobIdentity(job_id, "mcp-provider", "op-restart", "idem-restart"),
            (sys.executable, "-c", "import time; time.sleep(30)"),
            cwd=tmp_path,
            max_descendants=4,
        ))
        assert first_registry.poll(job_id).status is JobStatus.PENDING
        first_registry.transition(job_id, JobStatus.RUNNING)

        # A new registry/provider owner sees only the durable record and the
        # persisted process identity, which models a process restart.
        reopened = SQLiteDurableJobRegistry(database)
        report = reopened.reconcile_with_cleanup(
            cleanup,
            owner_instance_id="old-instance",
            owner_alive=False,
            max_process_descendants=4,
        )

        assert report.interrupted_job_ids == (job_id,)
        assert report.cleanup_receipts[0].requested is True
        assert report.cleanup_receipts[0].complete is True
        assert reopened.poll(job_id).status is JobStatus.INTERRUPTED
    finally:
        process = provider._processes.get(job_id)
        if process is not None and process.poll() is None:
            provider.cancel(job_id, reason="test cleanup")
