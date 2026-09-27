"""Composition registers the subagent-dispatch verifier (#515, no server start)."""

from __future__ import annotations


def test_worker_effect_journal_registers_the_subagent_dispatch_verifier(tmp_path):
    from sonder_runtime.adapters.execution.subagent_dispatch_verifier import (
        DurableSubagentDispatchVerifier,
    )
    from sonder_runtime.adapters.subagents import LocalSubagentProvider
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    application = build_application(
        config=SonderConfig(state=StateConfig(home=str(tmp_path)))
    )
    journal = application.process_job_provider()._effect_binding.journal
    verifiers = journal._reconciliation_verifiers
    assert set(verifiers) >= {"process-start", "compute-submit", "subagent-dispatch"}
    dispatch = verifiers["subagent-dispatch"]
    assert isinstance(dispatch, DurableSubagentDispatchVerifier)
    assert dispatch.operation_ids == frozenset({"subagent-dispatch"})

    # The provider journals dispatch in the same worker-effects journal and
    # reads receipts from the same durable child store the verifier uses.
    delegation = application.delegation_service()
    try:
        provider = delegation._provider
        assert isinstance(provider, LocalSubagentProvider)
        assert isinstance(provider._dispatch_verifier, DurableSubagentDispatchVerifier)
        repository = provider._local_service._repository
        assert dispatch._repository_getter() is repository
        assert provider._dispatch_verifier._repository_getter() is repository
    finally:
        application.close_delegation(timeout=5)
