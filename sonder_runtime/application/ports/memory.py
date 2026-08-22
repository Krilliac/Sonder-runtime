"""Application ports for the provider-neutral memory boundary."""
from __future__ import annotations

from typing import Protocol

from ...domain.common.events import DomainEvent


class MemoryConnectionScope(Protocol):
    """The connection owned by one memory unit of work."""

    memory: "MemoryRepositoryPort"
    connection: object


class MemoryRepositoryPort(Protocol):
    """Write/read operations used by the memory application facade."""

    def get_interaction(self, interaction_id: str) -> dict | None: ...

    def record_outcome(
        self, interaction_id: str, signal: str, reward_value: float, *, source: str,
    ) -> None: ...

    def append_outbox_event(self, event: DomainEvent) -> None: ...

