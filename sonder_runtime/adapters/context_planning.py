"""Runtime adapter for the typed application context assembly path."""
from __future__ import annotations

from typing import Mapping

from sonder_runtime.application.context_integration import ContextAssembly, ContextAssemblyService
from sonder_runtime.application.context_planner import ModelContext
from sonder_runtime.domain.context.priority import ContextItem
from sonder_runtime.platform.context_selection import requested_context


class RuntimeContextPlanningAdapter:
    """Bind runtime context-size selection to the application use case."""

    def __init__(self, service: ContextAssemblyService | None = None) -> None:
        self._service = service or ContextAssemblyService()

    def assemble(
        self,
        *,
        model: str,
        items: Mapping[str, tuple[ContextItem, ...] | list[ContextItem]],
        section_budgets: Mapping[str, int],
        reserved_output_tokens: int,
        context_tokens: int | str | None = None,
    ) -> ContextAssembly:
        """Assemble one bounded runtime request from typed context candidates."""
        context_window_tokens = requested_context(context_tokens)
        return self._service.assemble(
            ModelContext(model, context_window_tokens, reserved_output_tokens),
            items,
            section_budgets,
        )


__all__ = ["RuntimeContextPlanningAdapter"]
