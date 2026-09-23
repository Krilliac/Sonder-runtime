"""Canonical memory repository adapter for the application persistence port.

The repository is bound to the SQLite connection owned by a unit of work.
Keeping that stateful boundary in its named adapter makes the composition
root explicit while the underlying memory-store implementation is migrated.
"""
from __future__ import annotations


class MemoryRepositoryAdapter:
    """Implement ``MemoryRepository`` over one UnitOfWork connection.

    A composition root may inject the narrow authoritative fact source.  The
    default remains the legacy store path until a later replication lifecycle
    slice explicitly composes that source.
    """

    def __init__(
        self,
        conn,
        *,
        authoritative_fact_source=None,
        begin_authoritative_transaction=None,
    ) -> None:
        if authoritative_fact_source is not None and (
            not callable(getattr(authoritative_fact_source, "add_fact", None))
            or not callable(getattr(authoritative_fact_source, "delete_fact", None))
        ):
            raise TypeError(
                "authoritative fact source must provide add_fact and delete_fact"
            )
        if begin_authoritative_transaction is not None and not callable(
            begin_authoritative_transaction
        ):
            raise TypeError("authoritative transaction starter must be callable")
        self._conn = conn
        self._authoritative_fact_source = authoritative_fact_source
        self._begin_authoritative_transaction = begin_authoritative_transaction

    def add_fact(self, fact_id: str, project: str, text: str, embedding=None, *, metadata=None) -> None:
        if self._authoritative_fact_source is not None:
            if self._begin_authoritative_transaction is not None:
                self._begin_authoritative_transaction()
            self._authoritative_fact_source.add_fact(
                self._conn, fact_id, project, text, embedding, metadata
            )
            return None
        import sonder_runtime.adapters.memory_store as memory_store

        if metadata is not None:
            raise ValueError("authoritative metadata requires the configured fact source")

        memory_store.add_fact(self._conn, fact_id, project, text, embedding)

    def delete_fact(self, fact_id: str, project: str) -> bool:
        if self._authoritative_fact_source is not None:
            if self._begin_authoritative_transaction is not None:
                self._begin_authoritative_transaction()
            return self._authoritative_fact_source.delete_fact(
                self._conn, fact_id, project
            )
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.delete_fact(self._conn, fact_id, project)

    def facts_for_project(self, project: str) -> list:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.facts_for_project(self._conn, project)

    def count_facts(self, project: str) -> int:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.count_facts(self._conn, project)

    def entities_for_project(self, project: str, *, entity_id: str | None = None, now: str | None = None, offset: int = 0) -> list[dict]:
        from .persistence.sqlite.authoritative_indexes import entities_for_project
        return entities_for_project(self._conn, project, entity_id=entity_id, now=now, offset=offset)

    def decisions_for_project(self, project: str, *, decision_id: str | None = None, now: str | None = None, offset: int = 0) -> list[dict]:
        from .persistence.sqlite.authoritative_indexes import decisions_for_project
        return decisions_for_project(self._conn, project, decision_id=decision_id, now=now, offset=offset)

    def rebuild_authoritative_indexes(self, project: str | None = None) -> int:
        from .persistence.sqlite.authoritative_indexes import rebuild_authoritative_fact_indexes
        return rebuild_authoritative_fact_indexes(self._conn, project=project)

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
