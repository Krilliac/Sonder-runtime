"""Adapters that bind concrete child runners to the application port."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
import uuid

from ..application.context import OperationContext
from ..application.ports.subagents import (
    InvalidSubagentRequest, SubagentHandle, SubagentProvider, SubagentRequest,
    SubagentSnapshot, SubagentBudget,
)
from ..application.subagents.durable_continuation import (
    DurableCancellation, DurableContinuationService, ContinuableCheckpoint,
)
from ..application.execution.effect_journal import bound as bound_effect_journal
from ..application.execution.worker_bindings import (
    AuthenticatedWorkerBinding, journaled_effect,
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
        effect_binding_factory: Callable[[SubagentRequest, OperationContext], AuthenticatedWorkerBinding] | None = None,
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
        if effect_binding_factory is not None and not callable(effect_binding_factory):
            raise TypeError("effect_binding_factory must be callable")
        self._effect_binding_factory = effect_binding_factory

    def register_root(self, root_id: str, budget: SubagentBudget) -> None:
        self._local_service.register_root(root_id, budget)

    def spawn(self, request: SubagentRequest, context: OperationContext) -> SubagentHandle:
        """Apply request ceilings around the concrete local runner."""
        self._local_service.require_parent(request.parent_id)
        if request.child_id is None:
            request = replace(request, child_id="child-" + uuid.uuid4().hex)
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

            def invoke_runner():
                binding = (
                    self._effect_binding_factory(request, context)
                    if self._effect_binding_factory is not None else None
                )
                if binding is None:
                    return runner(state, bounded_save, control)
                if not isinstance(binding, AuthenticatedWorkerBinding):
                    raise TypeError("effect_binding_factory returned an invalid binding")
                with bound_effect_journal(binding.binding()):
                    return journaled_effect(
                        binding,
                        operation_id=f"subagent-run:{request.child_id}",
                        idempotency_key=request.idempotency_key or request.child_id,
                        request={
                            "child_id": request.child_id,
                            "parent_id": request.parent_id,
                            "prompt": request.prompt,
                        },
                        invoke=lambda: runner(state, bounded_save, control),
                        receipt_key=f"subagent:{request.child_id}",
                        reconciliation="manual",
                    )

            output = invoke_runner()
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
