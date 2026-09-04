"""Adapters that bind concrete child runners to the application port."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from ..application.context import OperationContext
from ..application.ports.subagents import (
    InvalidSubagentRequest, SubagentHandle, SubagentProvider, SubagentRequest,
    SubagentSnapshot, SubagentBudget,
)
from ..application.subagents.durable_continuation import (
    DurableCancellation, DurableContinuationService, ContinuableCheckpoint,
)


Runner = Callable[[Mapping[str, object], Callable[[Mapping[str, object], str | None], ContinuableCheckpoint], DurableCancellation], str]


class RunnerBoundSubagentProvider:
    """Expose a durable continuation service through ``SubagentProvider``."""

    def __init__(self, service: DurableContinuationService, runner: Runner) -> None:
        self._service = service
        self._runner = runner

    def spawn(self, request: SubagentRequest, context: OperationContext) -> SubagentHandle:
        return self._service.spawn(request, context, self._runner)

    def snapshot(self, child_id: str) -> SubagentSnapshot:
        return self._service.snapshot(child_id)

    def cancel(self, child_id: str, *, reason: str = "cancellation requested") -> bool:
        return self._service.cancel(child_id, reason=reason)

    def close(self, timeout: float | None = None) -> bool:
        return self._service.close(timeout)


class UnsupportedSubagentProvider(InvalidSubagentRequest):
    """Raised when a caller asks the local adapter for an unconfigured backend."""


class LocalSubagentProvider(RunnerBoundSubagentProvider):
    """Provider-neutral child port backed by the local durable runner.

    The adapter owns the local provider choice and requires callers to publish
    an explicit durable root before spawning.  Runner output and checkpoint
    writes are bounded by the request budget; unsupported provider names fail
    before any child is published.
    """

    def __init__(
        self,
        service: DurableContinuationService,
        runner: Runner | None = None,
        *,
        runner_factory: Callable[[SubagentRequest, OperationContext], Runner] | None = None,
        provider: str = "local",
    ) -> None:
        if provider != "local":
            raise UnsupportedSubagentProvider(
                f"unsupported subagent provider: {provider!r}"
            )
        if runner is None and runner_factory is None:
            raise InvalidSubagentRequest("local provider requires a concrete runner")
        super().__init__(service, runner)
        self._runner_factory = runner_factory
        self._local_service = service

    def register_root(self, root_id: str, budget: SubagentBudget) -> None:
        self._local_service.register_root(root_id, budget)

    def spawn(self, request: SubagentRequest, context: OperationContext) -> SubagentHandle:
        """Apply request ceilings around the concrete local runner."""
        self._local_service.require_parent(request.parent_id)
        budget = request.budget
        runner = self._runner_factory(request, context) if self._runner_factory else self._runner

        def bounded_runner(state, save, control):
            steps = 0

            def bounded_save(next_state, cursor=None):
                nonlocal steps
                steps += 1
                if budget.max_steps is not None and steps > budget.max_steps:
                    raise TimeoutError("subagent step budget exhausted")
                return save(next_state, cursor)

            output = runner(state, bounded_save, control)
            if not isinstance(output, str):
                raise InvalidSubagentRequest("local runner output must be text")
            # Four UTF-8 characters is a conservative local token estimate;
            # the evidence envelope applies its own stricter bound downstream.
            if (
                budget.max_output_tokens is not None
                and len(output) > budget.max_output_tokens * 4
            ):
                raise TimeoutError("subagent output budget exhausted")
            return output

        return self._local_service.spawn(request, context, bounded_runner)


__all__ = [
    "LocalSubagentProvider", "RunnerBoundSubagentProvider",
    "UnsupportedSubagentProvider",
]
