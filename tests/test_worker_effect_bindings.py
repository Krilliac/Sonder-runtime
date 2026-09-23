from __future__ import annotations

from pathlib import Path

from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
from sonder_runtime.application.compute_fabric.jobs import ComputeJobWorker
from sonder_runtime.application.execution.worker_bindings import AuthenticatedWorkerBinding
from sonder_runtime.application.selfmod.selfmod_service import GuardedLegacySelfmodService


def test_compute_worker_journals_submit_at_worker_boundary(tmp_path, monkeypatch):
    # Reuse the real catalog and provider boundary without launching a process.
    from tests.test_compute_job_worker import CapturingProvider, _envelope, _entry

    monkeypatch.setattr(
        ComputeJobWorker,
        "_artifact_stage_base",
        staticmethod(lambda: tmp_path / "artifact-stages"),
    )
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = AuthenticatedWorkerBinding(
        journal, "compute-run", "compute-worker", 3, "/workspace",
    )
    worker = ComputeJobWorker(
        worker_id="compute-worker",
        catalog={"pytest": _entry()},
        workspace_mappings={"sonder": tmp_path},
        provider=CapturingProvider(),
        effect_binding=binding,
    )

    receipt = worker.submit(_envelope())

    stored = journal.get("compute-run:compute-submit:compute-worker:idem-1")
    assert stored is not None
    assert stored.worker_id == "compute-worker"
    assert stored.owner_epoch == 3
    assert stored.receipt_key == receipt.remote_job_id
    assert stored.state.value == "completed"


def test_process_provider_journals_launch_before_return(tmp_path):
    from tests.test_job004_process_provider import _Cleanup, _MemoryLimiter, _Process, _request
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from sonder_runtime.application.jobs.durable_registry import DurableJobRegistry

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = AuthenticatedWorkerBinding(
        journal, "process-run", "process-worker", 4, "/workspace",
    )
    provider = SubprocessJobProvider(
        DurableJobRegistry(),
        process_cleanup=_Cleanup(complete=True),
        launcher=lambda *args, **kwargs: _Process(),
        memory_limiter=_MemoryLimiter(),
        process_identity_resolver=lambda _pid: "stable",
        platform_name="posix",
        effect_binding=binding,
    )

    started = provider.start(_request("journaled-process"))

    stored = journal.get("process-run:process-start:journaled-process")
    assert stored is not None
    assert stored.worker_id == "process-worker"
    assert stored.owner_epoch == 4
    assert stored.receipt_key.startswith("journaled-process:")
    assert started.process_id > 0
    provider.wait("journaled-process")


def test_worker_binding_refuses_restart_with_orphaned_effect(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    old = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace")
    intent = old.binding().begin_request(
        operation_id="mutation", idempotency_key="mutation-1", request_digest="a" * 64,
    )
    assert intent.state.value == "intent"

    current = AuthenticatedWorkerBinding(journal, "run", "worker", 2, "/workspace")
    try:
        current.recover_before_restart()
    except ValueError as exc:
        assert "reconciliation" in str(exc)
    else:
        raise AssertionError("restart must refuse an orphaned effect")


def test_selfmod_deploy_journals_the_legacy_mutation_boundary(tmp_path):
    from tests.test_selfmod_legacy_integration import LegacyDouble

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    legacy = LegacyDouble()
    service = GuardedLegacySelfmodService(
        legacy,
        unrestricted=True,
        effect_binding_factory=lambda run_id: AuthenticatedWorkerBinding(
            journal, run_id, "selfmod-worker", 7, "/workspace",
        ),
    )
    service.create_plan("change", tmp_path)
    service.deploy("selfmod-test-1", commit=False)

    stored = journal.get("selfmod-test-1:selfmod-deploy:selfmod-test-1")
    assert stored is not None
    assert stored.worker_id == "selfmod-worker"
    assert stored.owner_epoch == 7
    assert stored.state.value == "completed"
    assert [name for name, _ in legacy.calls] == ["deploy"]


def test_composition_root_supplies_host_owned_bindings(tmp_path):
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    application = build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path)))
    )

    process = application.process_job_provider()
    process_binding = process._effect_binding
    assert process_binding is not None
    assert process_binding.worker_id.startswith("process:")
    assert process_binding.scope == "process-jobs"
    assert process_binding.run_id == "runtime:process-jobs"

    compute = application.compute_job_worker()
    compute_binding = compute._effect_binding
    assert compute_binding is not None
    assert compute_binding.worker_id.startswith("compute:")
    assert compute_binding.scope == "compute-jobs"
    assert compute_binding.run_id == "runtime:compute-jobs"

    selfmod = application.selfmod_service()
    selfmod_binding = selfmod._effect_binding_factory("run-1")
    assert selfmod_binding.worker_id.startswith("selfmod:")
    assert selfmod_binding.scope == "selfmod-mutation"
    assert selfmod_binding.run_id == "selfmod:run-1"
