"""Adapters that bind concrete child runners to the application port."""
from __future__ import annotations

from collections.abc import Callable, Mapping

from ..application.context import OperationContext
from ..application.ports.subagents import (
    SubagentHandle, SubagentProvider, SubagentRequest, SubagentSnapshot,
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


__all__ = ["RunnerBoundSubagentProvider"]
