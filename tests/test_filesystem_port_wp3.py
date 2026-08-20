from pathlib import Path

import pytest

from sonder_runtime.application.ports.filesystem import (
    FileSystem,
    FileSystemObservation,
    FileSystemOperation,
    FileSystemPolicyDecision,
    FileSystemRequest,
    FileSystemResource,
    PolicyEffect,
    ResourceKind,
    validate_request,
)
from sonder_runtime.domain.common.errors import CapacityExceeded, InvalidInput


def request(operation=FileSystemOperation.READ, **kwargs):
    return FileSystemRequest(
        operation=operation,
        resource=FileSystemResource(Path("workspace/data.txt")),
        **kwargs,
    )


def test_resource_and_operation_are_typed_and_policy_is_descriptive():
    req = request(FileSystemOperation.WRITE, content=b"hello")
    decision = FileSystemPolicyDecision(
        PolicyEffect.CONFIRM, "mutation requires confirmation", req.operation, req.resource,
        required_confirmation=True, max_bytes=10,
    )
    assert req.resource.kind is ResourceKind.FILE
    assert decision.required_confirmation
    assert decision.effect is PolicyEffect.CONFIRM


def test_observation_is_bounded_and_carries_policy_outcome():
    observation = FileSystemObservation(
        FileSystemOperation.READ,
        FileSystemResource(Path("workspace/data.txt")),
        PolicyEffect.ALLOW,
        succeeded=True,
        bytes_read=5,
        version="v1",
    )
    assert observation.bytes_read == 5
    assert observation.error_code is None


def test_request_validation_rejects_unbounded_write_and_invalid_move():
    with pytest.raises(CapacityExceeded):
        validate_request(request(FileSystemOperation.WRITE, content=b"123", max_bytes=2))
    with pytest.raises(InvalidInput):
        validate_request(request(FileSystemOperation.MOVE))


def test_port_is_protocol_only_and_has_all_resource_operations():
    assert FileSystem.__module__.endswith("filesystem")
    assert {item.value for item in FileSystemOperation} == {
        "read", "write", "delete", "list", "stat", "move",
    }
