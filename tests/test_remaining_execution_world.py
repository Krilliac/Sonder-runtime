from __future__ import annotations

import pytest

from sonder_runtime.application.execution.world_control import (
    BoundedOutputBuffer,
    ExecutionSurface,
    ExecutionWorldKind,
    InMemoryExecutionWorldController,
    IsolationClaim,
    IsolationTruth,
    OutputStream,
    OutputWatermark,
    SharedExecutionWorld,
    require_same_world,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus


def world(world_id="world-1"):
    return SharedExecutionWorld(
        world_id,
        ExecutionWorldKind.CONTAINER,
        frozenset(ExecutionSurface),
        IsolationClaim(IsolationTruth.FAILURE_ISOLATION_ONLY, "provider has no verified host boundary"),
        "fixture",
    )


def identity(job_id="job-1"):
    return JobIdentity(job_id, "code", "op-1", f"idem-{job_id}")


def test_all_execution_surfaces_bind_to_one_typed_world_and_mismatch_is_rejected():
    current = world()
    bindings = [current.bind(surface) for surface in ExecutionSurface]
    assert require_same_world(*bindings) == "world-1"
    other = world("world-2").bind(ExecutionSurface.SHELL)
    with pytest.raises(ValueError, match="same world"):
        require_same_world(bindings[0], other)


def test_isolation_truth_never_confuses_failure_isolation_with_security_boundary():
    claim = IsolationClaim(IsolationTruth.FAILURE_ISOLATION_ONLY, "child failure is contained")
    assert not claim.is_security_boundary
    with pytest.raises(ValueError, match="evidence"):
        IsolationClaim(IsolationTruth.SECURITY_BOUNDARY_VERIFIED, "secure")
    verified = IsolationClaim(IsolationTruth.SECURITY_BOUNDARY_VERIFIED, "container policy verified", "att-1")
    assert verified.is_security_boundary


def test_job_control_supports_start_poll_stream_cancel_and_terminal_collection():
    controller = InMemoryExecutionWorldController(world())
    started = controller.start(identity())
    assert started.status is JobStatus.RUNNING
    controller._output.append(OutputStream.STDOUT, "hello")  # reference adapter injection
    page = controller.stream("job-1", max_events=1)
    assert page.events[0].data == "hello"
    cancelled = controller.cancel("job-1")
    assert cancelled.status is JobStatus.CANCELLED
    assert controller.collect("job-1") == cancelled

    terminal = controller.open_terminal("term-1", columns=100, rows=30)
    assert terminal.world.world_id == started.world.world_id
    controller.send_terminal("term-1", "ready")
    assert controller.reconnect_terminal("term-1").terminal_id == "term-1"
    assert controller.resize_terminal("term-1", columns=120, rows=40).columns == 120
    assert controller.stop_terminal("term-1").stopped


def test_output_reads_are_bounded_and_watermarks_prevent_replay():
    buffer = BoundedOutputBuffer(max_events=3, max_bytes=100)
    first = buffer.append(OutputStream.STDOUT, "one")
    second = buffer.append(OutputStream.STDOUT, "two")
    page = buffer.read(first.watermark, max_events=1, max_bytes=3)
    assert [event.data for event in page.events] == ["two"]
    assert page.next_watermark == second.watermark
    assert not buffer.read(page.next_watermark).events

    buffer.append(OutputStream.STDOUT, "three")
    buffer.append(OutputStream.STDOUT, "four")
    buffer.append(OutputStream.STDOUT, "five")
    expired = buffer.read(OutputWatermark(0))
    assert expired.truncated is True


def test_terminal_lifecycle_rejects_reconnect_after_stop_and_bounds_dimensions():
    controller = InMemoryExecutionWorldController(world())
    controller.open_terminal("term-1")
    controller.stop_terminal("term-1")
    with pytest.raises(ValueError, match="stopped"):
        controller.reconnect_terminal("term-1")
    with pytest.raises(ValueError, match="positive"):
        controller.open_terminal("term-2", columns=0)
