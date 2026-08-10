"""Outcome recording service (SPEC-5 §15).

Records learning outcomes atomically: persists the outcome row and
appends a domain event to the transactional outbox in the same
SQLite transaction.
"""
from __future__ import annotations

from typing import Protocol

from ...domain.memory.rules import reward_score, reward_is_good, VALID_SIGNALS
from ...domain.common.events import DomainEvent
from ...domain.common.errors import InvalidInput


class OutcomeStore(Protocol):
    """Write-side port for outcome persistence."""

    def record_outcome(
        self, interaction_id: str, signal: str, reward: float,
    ) -> None: ...

    def get_interaction(self, interaction_id: str) -> dict | None: ...

    def append_outbox_event(self, event: DomainEvent) -> None: ...


class OutcomeService:
    """Records learning outcomes with atomic event emission."""

    def __init__(self, store: OutcomeStore):
        self._store = store

    def record(self, interaction_id: str, signal: str) -> float:
        if signal not in VALID_SIGNALS:
            raise InvalidInput(
                f"unknown signal {signal!r}; valid: {sorted(VALID_SIGNALS)}"
            )

        interaction = self._store.get_interaction(interaction_id)
        if interaction is None:
            raise InvalidInput(f"interaction {interaction_id!r} not found")

        reward = reward_score(signal)

        self._store.record_outcome(interaction_id, signal, reward)

        event = DomainEvent(
            event_type="outcome.recorded",
            aggregate_type="interaction",
            aggregate_id=interaction_id,
            sequence=0,
            payload={
                "signal": signal,
                "reward": reward,
                "is_good": reward_is_good(signal),
            },
        )
        self._store.append_outbox_event(event)

        return reward
