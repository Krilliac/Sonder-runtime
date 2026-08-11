"""Repository and unit-of-work ports (SPEC-3 R-M5/R-M6).

Each table and authoritative file has exactly one repository owner;
application use cases declare transaction boundaries through UnitOfWork.
Transport and domain code never open ad-hoc connections.
"""
from __future__ import annotations

from typing import Protocol

from .event_sink import EventSink


class MemoryRepository(Protocol):
    """Persistence port for the memory store, bound to a UnitOfWork connection.

    Methods mirror the root ``memory_store`` / ``recall`` surface so the
    strangler adapter is a faithful wrap. Instances share the UnitOfWork's
    connection and are created by it — not constructed standalone.
    """

    def add_fact(
        self, fact_id: str, project: str, text: str, embedding=None
    ) -> None: ...

    def facts_for_project(self, project: str) -> list[dict]: ...

    def count_facts(self, project: str) -> int: ...

    def log_interaction(
        self,
        interaction_id: str,
        task: str,
        retrieved_ctx,
        response: str,
        tier: str,
        **fields,
    ) -> None: ...

    def get_interaction(self, interaction_id: str) -> dict | None: ...

    def recall(
        self, task: str, *, k: int = 2, project: str | None = None, **options
    ) -> list: ...

    def record_outcome(
        self, interaction_id: str, signal: str, reward_value: float, *,
        source: str, **options
    ): ...


class AutomationRepository(Protocol):
    """Persistence port for durable, restart-safe autopilot runs.

    The one owner of the ``autopilot.db`` run ledger. Ownership is leased:
    a controller claims a run, heartbeats to keep the lease, and either
    finishes it or has it reconciled when the owner dies. Methods mirror the
    root ``autopilot_store`` surface so the strangler adapter is a faithful
    wrap; ``lease_seconds=None`` defers to the store's own default.
    """

    def create_run(
        self,
        objective: str,
        *,
        project: str = "",
        tier: str = "code",
        policy: str = "workspace",
        allow_web: bool = True,
        max_failures: int = 3,
        max_tasks: int = 12,
        max_replans: int = 2,
        adaptive: bool = True,
    ) -> dict: ...

    def get_run(self, selector: str = "") -> dict | None: ...

    def list_runs(
        self, include_finished: bool = True, limit: int = 20
    ) -> list[dict]: ...

    def claim_run(
        self,
        selector: str,
        owner_id: str,
        *,
        owner_pid: int,
        lease_seconds: int | None = None,
    ) -> dict | None: ...

    def save_progress(self, run_id: str, owner_id: str, **changes) -> dict | None: ...

    def heartbeat(
        self, run_id: str, owner_id: str, lease_seconds: int | None = None
    ) -> bool: ...

    def request_pause(self, selector: str) -> dict | None: ...

    def request_cancel(self, selector: str) -> dict | None: ...

    def control_flags(self, run_id: str, owner_id: str) -> dict: ...

    def finish_run(
        self,
        run_id: str,
        owner_id: str,
        status: str,
        *,
        summary: str = "",
        final_report: str = "",
        last_error: str = "",
    ) -> dict | None: ...

    def reconcile_stale_runs(self, now: float | None = None) -> int: ...

    def events(self, selector: str = "", limit: int = 20) -> list[dict]: ...

    def snapshot(
        self, include_finished: bool = True, limit: int = 20
    ) -> dict: ...


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
