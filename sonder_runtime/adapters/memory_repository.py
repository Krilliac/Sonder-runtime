"""Canonical memory repository adapter for the application persistence port.

The repository is bound to the SQLite connection owned by a unit of work.
Keeping that stateful boundary in its named adapter makes the composition
root explicit while the underlying memory-store implementation is migrated.
"""
from __future__ import annotations


class MemoryRepositoryAdapter:
    """Implement ``MemoryRepository`` over one UnitOfWork connection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def add_fact(self, fact_id: str, project: str, text: str, embedding=None) -> None:
        import sonder_runtime.adapters.memory_store as memory_store

        memory_store.add_fact(self._conn, fact_id, project, text, embedding)

    def delete_fact(self, fact_id: str, project: str) -> bool:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.delete_fact(self._conn, fact_id, project)

    def facts_for_project(self, project: str) -> list:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.facts_for_project(self._conn, project)

    def count_facts(self, project: str) -> int:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.count_facts(self._conn, project)

    def log_interaction(
        self,
        interaction_id: str,
        task: str,
        retrieved_ctx,
        response: str,
        tier: str,
        **fields,
    ) -> None:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.log_interaction(
            self._conn, interaction_id, task, retrieved_ctx, response, tier, **fields
        )

    def get_interaction(self, interaction_id: str) -> dict | None:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.get_interaction(self._conn, interaction_id)

    def append_outbox_event(self, event) -> None:
        """Append a memory-domain event on the caller's transaction."""
        from .persistence.sqlite.outbox import OutboxWriter

        OutboxWriter(self._conn).append(event)

    def recall(self, task: str, *, k: int = 2, project: str | None = None, **options):
        import sonder_runtime.adapters.recall as recall_module

        return recall_module.recall(self._conn, task, k=k, project=project, **options)

    def record_outcome(
        self,
        interaction_id: str,
        signal: str,
        reward_value: float,
        *,
        source: str,
        **options,
    ):
        """Record a verdict with the required evidence source."""
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.record_outcome_and_claim_lesson_distillation(
            self._conn,
            interaction_id,
            signal,
            reward_value,
            source=source,
            **options,
        )
