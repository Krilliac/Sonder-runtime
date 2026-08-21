from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.adapters.subagents import RunnerBoundSubagentProvider
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import SubagentBudget, SubagentRequest, SubagentStatus
from sonder_runtime.application.subagents.durable_continuation import DurableContinuationService


def test_runner_bound_provider_adapts_durable_service(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    service = DurableContinuationService(repository)

    def runner(state, save, control):
        save({"step": 1}, "done")
        return "child output"

    provider = RunnerBoundSubagentProvider(service, runner)
    request = SubagentRequest("parent-1", "do work", SubagentBudget(max_steps=2), "child-1")
    handle = provider.spawn(request, local_owner_context(correlation_id="subagent-test"))
    result = handle.result(timeout=2)

    assert result.status is SubagentStatus.SUCCEEDED
    assert result.output == "child output"
    assert provider.snapshot("child-1").status is SubagentStatus.SUCCEEDED
    assert provider.close(timeout=1) is True
