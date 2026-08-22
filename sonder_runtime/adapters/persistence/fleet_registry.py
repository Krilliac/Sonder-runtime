"""Fleet-store adapter for the unified AGENT-001 registry seam."""
from __future__ import annotations

import os
import time
import uuid
from importlib import import_module


def _store():
    # Keep the persistence compatibility module behind this adapter's
    # runtime boundary; the architecture graph must not connect sibling
    # persistence repositories through package imports.
    return import_module("sonder_runtime.adapters.persistence.fleet_store")


class FleetStoreRegistryAdapter:
    """Persist both Fleet and Autopilot launch envelopes in fleet's ledger."""

    def __init__(self, *, owner_id: str | None = None, owner_pid: int | None = None) -> None:
        self.owner_id = owner_id or f"registry-{uuid.uuid4().hex[:12]}"
        self.owner_pid = int(owner_pid or os.getpid())
        _store().register_owner(self.owner_id, self.owner_pid)

    def create(self, launch) -> dict:
        metadata = dict(launch.metadata)
        row = {
            "id": launch.agent_id,
            "role": metadata.get("role", "agent"),
            "parent_id": launch.parent_id or "",
            "task": launch.prompt,
            "status": "queued",
            "started_ts": time.time(),
            "mode": launch.mode.value,
            "tier": metadata.get("tier", "code"),
            "project": metadata.get("project", ""),
            "delegated_task_digest": metadata.get("delegated_task_digest", ""),
        }
        return _store().create_agent(row, self.owner_id, self.owner_pid)

    def status(self, agent_id: str) -> dict | None:
        return _store().get_agent(agent_id)

    def resume(self, agent_id: str) -> dict:
        current = _store().get_agent(agent_id)
        if current is None:
            return None  # type: ignore[return-value]
        if current.get("status") == "interrupted":
            return {**current, "restart_required": True}
        return current

    def cancel(self, agent_id: str, *, reason: str = "") -> dict:
        return _store().cancel_agents(agent_id)

    def stop(self, agent_id: str, *, reason: str = "") -> dict:
        return _store().cancel_agents(agent_id)


__all__ = ["FleetStoreRegistryAdapter"]
