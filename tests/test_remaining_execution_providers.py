from dataclasses import dataclass

import pytest

from sonder_runtime.application.execution.world_control import ExecutionWorldKind
from sonder_runtime.application.execution.world_providers import (
    ConfiguredRemoteWorld,
    ContainerWorldConfig,
    GuardedContainerWorld,
    RemoteWorldConfig,
)


@dataclass
class Worker:
    calls: list[tuple[str, str]]

    def submit(self, *, world_id: str, payload: str) -> str:
        self.calls.append((world_id, payload))
        return f"receipt:{world_id}"


def test_container_is_described_but_denies_unconfigured_execution() -> None:
    provider = GuardedContainerWorld(ContainerWorldConfig("c-1", "python:3.12"))
    assert provider.world.kind is ExecutionWorldKind.CONTAINER
    with pytest.raises(PermissionError):
        provider.submit("echo hi")


def test_container_requires_explicit_worker_and_records_world_identity() -> None:
    worker = Worker([])
    provider = GuardedContainerWorld(ContainerWorldConfig("c-2", "img", allowed=True), worker)
    assert provider.submit("job") == "receipt:c-2"
    assert worker.calls == [("c-2", "job")]


def test_remote_denies_until_configured() -> None:
    provider = ConfiguredRemoteWorld(RemoteWorldConfig("r-1", "worker-a", "https://worker"))
    assert provider.world.kind is ExecutionWorldKind.REMOTE
    with pytest.raises(PermissionError):
        provider.submit("job")


def test_remote_routes_to_configured_worker_with_stable_identity() -> None:
    worker = Worker([])
    provider = ConfiguredRemoteWorld(RemoteWorldConfig("r-2", "worker-a", "https://worker", True), worker)
    assert provider.submit("job") == "receipt:r-2"
    assert worker.calls == [("r-2", "job")]
