"""Repository and unit-of-work ports (SPEC-3 R-M5/R-M6).

Each table and authoritative file has exactly one repository owner;
application use cases declare transaction boundaries through UnitOfWork.
Transport and domain code never open ad-hoc connections.
"""
from __future__ import annotations

from typing import Protocol

from .event_sink import EventSink


class MemoryRepository(Protocol):
    def remember_fact(self, principal_id: str, fact: str, *, source: str) -> str: ...

    def recall(self, principal_id: str, query: str, *, limit: int = 8) -> list: ...

    def record_outcome(self, interaction_id: str, outcome: str) -> None: ...


class AutomationRepository(Protocol):
    def claim_next_task(self, owner_id: str) -> dict | None: ...

    def transition_run(
        self, run_id: str, *, expected: str, next_status: str, revision: int
    ) -> dict: ...

    def heartbeat(self, run_id: str, owner_id: str) -> None: ...


class PolicyRepository(Protocol):
    def load(self) -> dict: ...

    def update(
        self,
        *,
        local_models: dict | None = None,
        routing: dict | None = None,
        npu: dict | None = None,
        expected_revision: int | None = None,
        source: str = "application",
    ) -> dict: ...


class UnitOfWork(Protocol):
    memory: MemoryRepository
    automation: AutomationRepository
    policy: PolicyRepository
    events: EventSink

    def __enter__(self) -> "UnitOfWork": ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def __exit__(self, exc_type, exc, tb) -> None: ...
