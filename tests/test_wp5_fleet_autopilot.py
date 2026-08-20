"""WP5 AGENT-001/002 focused Fleet and Autopilot adapter tests."""
from __future__ import annotations

import pytest

from sonder_runtime.application.agent_registry.fleet_autopilot import (
    AgentLaunch,
    AgentMode,
    AgentRegistryError,
    FleetAutopilotAdapter,
)


class Registry:
    def __init__(self):
        self.calls = []

    def create(self, launch):
        self.calls.append(("create", launch))
        return launch.agent_id

    def resume(self, agent_id):
        self.calls.append(("resume", agent_id))
        return agent_id

    def cancel(self, agent_id, *, reason=""):
        self.calls.append(("cancel", agent_id, reason))
        return True

    def stop(self, agent_id, *, reason=""):
        self.calls.append(("stop", agent_id, reason))
        return True

    def status(self, agent_id):
        self.calls.append(("status", agent_id))
        return "running"


def test_fleet_and_autopilot_share_one_canonical_launch_envelope():
    registry = Registry()
    adapter = FleetAutopilotAdapter(registry)

    adapter.launch_fleet(
        agent_id="fleet-1", operation_id="op-f", prompt="inspect", idempotency_key="i-f",
        metadata={"z": "last", "role": "worker"},
    )
    adapter.launch_autopilot(
        agent_id="auto-1", operation_id="op-a", prompt="continue", idempotency_key="i-a",
        parent_id="session-1",
    )

    fleet = registry.calls[0][1]
    autopilot = registry.calls[1][1]
    assert isinstance(fleet, AgentLaunch)
    assert (fleet.mode, fleet.metadata) == (AgentMode.FLEET, (("role", "worker"), ("z", "last")))
    assert (autopilot.mode, autopilot.parent_id) == (AgentMode.AUTOPILOT, "session-1")


def test_lifecycle_operations_delegate_without_mode_specific_loop():
    registry = Registry()
    adapter = FleetAutopilotAdapter(registry)

    assert adapter.resume("a") == "a"
    assert adapter.cancel("a", reason="operator") is True
    assert adapter.stop("a", reason="shutdown") is True
    assert adapter.status("a") == "running"
    assert [call[0] for call in registry.calls] == ["resume", "cancel", "stop", "status"]


@pytest.mark.parametrize("field", ["agent_id", "operation_id", "prompt", "idempotency_key"])
def test_launch_rejects_missing_identity_fields(field):
    registry = Registry()
    adapter = FleetAutopilotAdapter(registry)
    values = dict(agent_id="a", operation_id="o", prompt="p", idempotency_key="i")
    values[field] = " "  # type: ignore[index]
    with pytest.raises(AgentRegistryError, match=field):
        adapter.launch_fleet(**values)


def test_metadata_is_immutable_and_parent_is_preserved():
    registry = Registry()
    metadata = {"role": "reviewer"}
    FleetAutopilotAdapter(registry).launch_autopilot(
        agent_id="a", operation_id="o", prompt="p", idempotency_key="i",
        parent_id="parent", metadata=metadata,
    )
    metadata["role"] = "mutated"
    launch = registry.calls[0][1]
    assert launch.parent_id == "parent"
    assert launch.metadata == (("role", "reviewer"),)


def test_direct_envelope_rejects_empty_idempotency_key():
    with pytest.raises(AgentRegistryError, match="idempotency_key"):
        AgentLaunch("a", AgentMode.FLEET, "o", "p", idempotency_key="")
