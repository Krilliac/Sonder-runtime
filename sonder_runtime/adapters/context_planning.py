"""Runtime adapter for the typed application context assembly path."""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from sonder_runtime.application.context_integration import ContextAssembly, ContextAssemblyService, ContextPlanningFacade
from sonder_runtime.application.context_planner import ModelContext
from sonder_runtime.application.context_manifests import ContextRecord
from sonder_runtime.domain.context.hardware_sizing import MeasuredContextCapability
from sonder_runtime.domain.context.priority import ContextItem
from sonder_runtime.platform.context_selection import requested_context


class RuntimeContextPlanningAdapter:
    """Bind runtime context-size selection to the application use case."""

    def __init__(self, service: ContextAssemblyService | None = None) -> None:
        self._facade = ContextPlanningFacade(assembly_service=service)

    @property
    def facade(self) -> ContextPlanningFacade:
        """The provider-neutral application boundary used by this adapter."""
        return self._facade

    def assemble(
        self,
        *,
        model: str,
        items: Mapping[str, tuple[ContextItem, ...] | list[ContextItem]],
        section_budgets: Mapping[str, int],
        reserved_output_tokens: int,
        context_tokens: int | str | None = None,
        capability: MeasuredContextCapability | None = None,
        records: Sequence[ContextRecord] = (),
        prefix_version: str = "1",
        request_id: str | None = None,
        replay_metadata: Mapping[str, Any] | None = None,
    ) -> ContextAssembly:
        """Assemble one bounded runtime request from typed context candidates."""
        context_window_tokens = requested_context(context_tokens)
        return self._facade.assemble(
            ModelContext(model, context_window_tokens, reserved_output_tokens),
            items,
            section_budgets,
            capability=capability,
            records=records,
            prefix_version=prefix_version,
            request_id=request_id,
            replay_metadata=replay_metadata,
        )


__all__ = ["RuntimeContextPlanningAdapter"]
