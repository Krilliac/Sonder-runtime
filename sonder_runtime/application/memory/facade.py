"""Provider-neutral memory/learning facade for live entrypoints.

The facade owns transaction scoping and composes the existing application
services. It deliberately does not create embeddings or expose the SQLite
adapter to transport handlers.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from ...domain.memory.wp6_typed import TypedMemory
from .memory_policy import (
    MemoryClass,
    MemoryPolicy,
    RetrievalDecision,
    WriteDecision,
)
from .outcome_service import OutcomeService
from .procedural_learning import ProceduralLearningService, PromotionCandidate


class MemoryLearningFacade:
    """Single application boundary for recall, outcomes, and typed policy.

    ``unit_of_work`` is a factory because every call gets an isolated
    connection and therefore an atomic outcome/outbox transaction.
    """

    def __init__(
        self,
        unit_of_work: Callable,
        *,
        recall_service=None,
        policy: MemoryPolicy | None = None,
        procedural_learning: ProceduralLearningService | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._recall = recall_service
        self.policy = policy or MemoryPolicy()
        self._procedural_learning = procedural_learning or ProceduralLearningService()

    def recall(self, task: str, **options) -> list[str]:
        """Recall through the injected application service and bounded store."""
        if self._recall is None:
            raise RuntimeError("memory recall service is not configured")
        with self._unit_of_work() as scope:
            return self._recall.retrieve(scope.connection, task, **options)

    def record(self, interaction_id: str, signal: str, *, source: str = "caller") -> float:
        """Record an outcome and its outbox event in one unit of work."""
        with self._unit_of_work() as scope:
            return OutcomeService(scope.memory).record(
                interaction_id, signal, source=source,
            )

    # Explicit alias used by HTTP/MCP/CLI outcome handlers.
    record_outcome = record

    def evaluate_write(self, memory_class: MemoryClass | str, **kwargs) -> WriteDecision:
        return self.policy.write(memory_class, **kwargs)

    def evaluate_retrieval(
        self, memory_id: str, memory_class: MemoryClass | str, **kwargs
    ) -> RetrievalDecision:
        return self.policy.retrieve(memory_id, memory_class, **kwargs)

    def promotion_candidates(
        self, memories: list[TypedMemory] | tuple[TypedMemory, ...], *, now: datetime | None = None,
    ) -> list[PromotionCandidate]:
        return self._procedural_learning.candidates(memories, now=now)


__all__ = ["MemoryLearningFacade"]
