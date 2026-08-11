"""Outcome recording service (SPEC-5 §15).

Records learning outcomes atomically: persists the outcome row and
appends a domain event to the transactional outbox in the same
SQLite transaction.
"""
from __future__ import annotations

from typing import Protocol

from ...domain.memory.rules import (
    reward_score, reward_is_good, VALID_SIGNALS,
    OUTCOME_SOURCES, OUTCOME_SOURCE_CALLER,
)
from ...domain.common.events import DomainEvent
from ...domain.common.errors import InvalidInput


class OutcomeStore(Protocol):
    """Write-side port for outcome persistence."""

    def record_outcome(
        self, interaction_id: str, signal: str, reward: float, *, source: str,
    ) -> None: ...

    def get_interaction(self, interaction_id: str) -> dict | None: ...

    def append_outbox_event(self, event: DomainEvent) -> None: ...


class OutcomeService:
    """Records learning outcomes with atomic event emission.

    ``source`` is provenance (#62) and is fixed by the CALLER OF THIS SERVICE,
    never by the tool arguments: this service sits behind the caller-facing
    ``record_outcome`` MCP handler, so its default is `caller`, and a machine
    attribution path must pass its own value rather than reuse this entry.
    """

    def __init__(self, store: OutcomeStore):
        self._store = store

    def record(
        self, interaction_id: str, signal: str,
        source: str = OUTCOME_SOURCE_CALLER,
    ) -> float:
        if signal not in VALID_SIGNALS:
            raise InvalidInput(
                f"unknown signal {signal!r}; valid: {sorted(VALID_SIGNALS)}"
            )
        if source not in OUTCOME_SOURCES:
            raise InvalidInput(
                f"unknown outcome source {source!r}; "
                f"valid: {sorted(OUTCOME_SOURCES)}"
            )

        interaction = self._store.get_interaction(interaction_id)
        if interaction is None:
            raise InvalidInput(f"interaction {interaction_id!r} not found")

        reward = reward_score(signal)

        self._store.record_outcome(interaction_id, signal, reward, source=source)

        event = DomainEvent(
            event_type="outcome.recorded",
            aggregate_type="interaction",
            aggregate_id=interaction_id,
            sequence=0,
            payload={
                "signal": signal,
                "reward": reward,
                "is_good": reward_is_good(signal),
                "source": source,
            },
        )
        self._store.append_outbox_event(event)

        return reward
