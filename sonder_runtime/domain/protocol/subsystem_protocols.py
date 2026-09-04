"""PEP 544 Protocol interfaces for cross-subsystem store contracts.

Each protocol captures the public methods that other subsystems actually call,
not the full internal API of the concrete module.  This keeps subsystem
boundaries explicit: composition.py, server.py, and master_orchestrator.py
depend on these abstractions rather than importing concrete persistence modules
directly.

Usage::

    from sonder_runtime.domain.protocol.subsystem_protocols import (
        AutopilotStore,
        FleetStore,
        GoalStore,
        CompositionStore,
    )

All protocols are ``@runtime_checkable`` so ``isinstance()`` works for
structural conformance checks in tests and wiring code.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# AutopilotStore
# ---------------------------------------------------------------------------

@runtime_checkable
class AutopilotStore(Protocol):
    """Contract satisfied by ``sonder_runtime.adapters.persistence.autopilot_store``.

    Covers the methods called from server.py, composition.py, and the
    autopilot controller across subsystem boundaries.
    """

    def database_path(self) -> str: ...

    def create_run(
        self,
        objective: str,
        *,
        project: str = "",
        request_owner: str = "",
        tier: str = "code",
        policy: str = "workspace",
        allow_web: bool = True,
        max_failures: int = 3,
        max_tasks: int = 12,
        max_replans: int = 2,
        adaptive: bool = True,
    ) -> dict: ...

    def get_run(
        self, selector: str = "", request_owner: str | None = None,
    ) -> dict | None: ...

    def list_runs(
        self,
        include_finished: bool = True,
        limit: int = 20,
        request_owner: str | None = None,
    ) -> list[dict]: ...

    def request_pause(
        self, selector: str, request_owner: str | None = None,
    ) -> dict | None: ...

    def request_cancel(
        self, selector: str, request_owner: str | None = None,
    ) -> dict | None: ...

    def attach_steering(
        self,
        selector: str,
        message: str,
        *,
        kind: str = "guidance",
        request_owner: str | None,
    ) -> dict | None: ...

    def heartbeat(
        self,
        run_id: str,
        owner_id: str,
        lease_seconds: int = ...,
    ) -> bool: ...

    def snapshot(
        self,
        include_finished: bool = True,
        limit: int = 20,
        request_owner: str | None = None,
    ) -> dict: ...


# ---------------------------------------------------------------------------
# FleetStore
# ---------------------------------------------------------------------------

@runtime_checkable
class FleetStore(Protocol):
    """Contract satisfied by ``sonder_runtime.adapters.persistence.fleet_store``.

    Covers the methods called from server.py and master_orchestrator.py
    across subsystem boundaries.
    """

    def database_path(self) -> str: ...

    def register_principal(self, principal_id: str, secret: str) -> None: ...

    def local_principal_credentials(self) -> tuple[str, str]: ...

    def register_owner(
        self, owner_id: str, pid: int, started_ts: float | None = None,
    ) -> None: ...

    def heartbeat_owner(self, owner_id: str) -> bool: ...

    def close_owner(
        self, owner_id: str, reason: str = "process exited before completion",
    ) -> int: ...

    def reconcile_stale_owners(
        self,
        *,
        now: float | None = None,
        stale_seconds: int = ...,
        grace_seconds: int = ...,
    ) -> dict: ...

    def create_agent(
        self,
        row: dict,
        owner_id: str,
        owner_pid: int,
        *,
        principal_id: str = "",
        principal_secret: str = "",
    ) -> dict: ...

    def start_agent(
        self,
        agent_id: str,
        owner_id: str,
        activity: str,
        *,
        in_model_call: bool = False,
        tool_calls: int = 0,
        requested_agents: int = 0,
        worker_slots: int = 0,
        mode: str = "",
        tier: str = "",
    ) -> dict | None: ...

    def begin_model_call(
        self,
        agent_id: str,
        owner_id: str,
        activity: str,
        *,
        tool_calls: int,
    ) -> dict | None: ...

    def update_agent(
        self, agent_id: str, owner_id: str, **changes: object,
    ) -> dict | None: ...

    def finish_agent(
        self,
        agent_id: str,
        owner_id: str,
        *,
        output: str = "",
        error: str = "",
        task_drift: bool = False,
        drift_metrics: dict | None = None,
    ) -> tuple[dict | None, str]: ...

    def cancel_agents(self, selector: str) -> dict: ...

    def cancellation_requested(self, agent_id: str) -> bool: ...

    def get_agent(self, selector: str, *, role: str = "") -> dict | None: ...

    def list_agents_scoped(
        self,
        owner_id: str = "",
        *,
        project: str = "",
        parent_id: str = "",
        include_finished: bool = True,
        limit: int = 50,
        principal_id: str = "",
        principal_secret: str = "",
    ) -> list[dict]: ...

    def queue_agent_message(
        self,
        sender_id: str,
        recipient_id: str,
        owner_id: str,
        *,
        project: str = "",
        mode: str,
        body: str,
        now: float | None = None,
        pending_ttl_seconds: int = ...,
        principal_id: str = "",
        principal_secret: str = "",
    ) -> dict: ...

    def claim_agent_messages(
        self,
        recipient_id: str,
        owner_id: str,
        *,
        project: str = "",
        limit: int = 8,
        now: float | None = None,
        principal_id: str = "",
        principal_secret: str = "",
    ) -> list[dict]: ...

    def add_event(
        self, agent_id: str, owner_id: str, stamp: str, message: str,
    ) -> None: ...

    def acquire_retry_lease(
        self,
        agent_id: str,
        *,
        lease_seconds: int = ...,
        now: float | None = None,
    ) -> dict | None: ...

    def release_retry_lease(self, agent_id: str, token: str) -> bool: ...

    def snapshot(
        self, include_finished: bool = True, limit: int = 20,
    ) -> dict: ...

    def prune(
        self,
        finished_retention: int = ...,
        event_retention: int = ...,
        message_retention_seconds: int = ...,
    ) -> dict: ...

    def clear_all(self) -> None: ...


# ---------------------------------------------------------------------------
# GoalStore
# ---------------------------------------------------------------------------

@runtime_checkable
class GoalStore(Protocol):
    """Contract satisfied by the top-level ``goal_store`` module.

    Covers the methods called from server.py and composition.py across
    subsystem boundaries.
    """

    def set_goal(
        self,
        objective: str,
        criteria: object = (),
        scope: str = "",
        origin: str = "user",
    ) -> dict: ...

    def get_active(self, scope: str = "") -> dict | None: ...

    def add_note(self, text: str, scope: str = "") -> dict | None: ...

    def complete(
        self, reason: str = "", scope: str = "", actor: str = "",
    ) -> dict: ...

    def abandon(
        self, reason: str = "", scope: str = "", actor: str = "",
    ) -> dict: ...

    def propose(
        self,
        objective: str,
        criteria: object = (),
        scope: str = "",
        source: str = "",
    ) -> dict | None: ...

    def proposals(self, scope: str = "", limit: int = 20) -> list: ...

    def adopt(
        self, goal_id: str, scope: str = "", actor: str = "",
    ) -> dict: ...

    def decline(self, goal_id: str, actor: str = "") -> dict: ...

    def get(self, goal_id: str) -> dict | None: ...

    def history(self, limit: int = 10, scope: str = "") -> list: ...

    def context_block(self, scope: str = "") -> str: ...


# ---------------------------------------------------------------------------
# CompositionStore
# ---------------------------------------------------------------------------

@runtime_checkable
class CompositionStore(Protocol):
    """Contract satisfied by ``sonder_runtime.adapters.persistence.composition_store``.

    Covers the methods called from composition.py and server.py across
    subsystem boundaries.
    """

    def bind(
        self,
        source_type: str,
        source_id: str,
        target_type: str,
        target_id: str,
        kind: str = "drives",
        metadata: dict | None = None,
    ) -> dict: ...

    def lookup_targets(
        self,
        source_type: str,
        source_id: str,
        target_type: str = "",
    ) -> list[dict]: ...

    def lookup_sources(
        self,
        target_type: str,
        target_id: str,
        source_type: str = "",
    ) -> list[dict]: ...

    def complete_binding(
        self, binding_id: str, reason: str = "",
    ) -> dict: ...

    def break_binding(
        self, binding_id: str, reason: str = "",
    ) -> dict: ...

    def active_bindings(self, limit: int = 50) -> list[dict]: ...

    def close_all_for(
        self,
        entity_type: str,
        entity_id: str,
        status: str = "completed",
    ) -> int: ...
