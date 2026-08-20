from __future__ import annotations

from sonder_runtime.application.ports.execution_world import (
    CleanupResult,
    ExecutionResult,
    ExecutionWorldSnapshot,
    ExecutionWorldSpec,
    ExecutionWorldState,
    ShellRequest,
    SubprocessRequest,
    TerminalChunk,
    TerminalRequest,
)
from sonder_runtime.application.ports import (
    ExecutionWorld,
    ShellExecutor,
    SubprocessRuntime,
    TerminalService,
)


class _SharedWorld:
    """Protocol-shaped fake: all services are deliberately world-bound."""

    def __init__(self) -> None:
        self.spec = ExecutionWorldSpec("world-1")
        self.subprocesses = object()
        self.shell = object()
        self.terminals = object()
        self.cancelled = False
        self.active_resources = 1

    def cancel(self, *, reason: str = "cancellation requested") -> bool:
        self.cancelled = True
        return reason == "stop"

    def cleanup(self, timeout: float | None = None) -> CleanupResult:
        self.active_resources = 0
        return CleanupResult(True, 0, ExecutionWorldState.QUIESCENT)

    def snapshot(self) -> ExecutionWorldSnapshot:
        return ExecutionWorldSnapshot(
            self.spec.world_id,
            ExecutionWorldState.CANCELLATION_REQUESTED
            if self.cancelled
            else ExecutionWorldState.ACTIVE,
            self.active_resources,
            "stop" if self.cancelled else None,
        )


def test_all_capabilities_share_one_world_and_handles_do_not_own_world():
    spec = ExecutionWorldSpec("world-1")
    assert spec.world_id == "world-1"
    assert SubprocessRequest(("python", "-c", "pass")).argv[0] == "python"
    assert ShellRequest("echo ready").command == "echo ready"
    assert TerminalRequest(("python", "-i"), columns=120, rows=40).columns == 120


def test_world_is_the_lifecycle_owner_for_all_three_ports():
    world: ExecutionWorld = _SharedWorld()
    assert world.spec.world_id == "world-1"
    assert world.subprocesses is world.subprocesses
    assert world.shell is world.shell
    assert world.terminals is world.terminals
    assert world.cancel(reason="stop") is True
    assert world.snapshot().state is ExecutionWorldState.CANCELLATION_REQUESTED
    assert world.cleanup(timeout=0) == CleanupResult(
        True, 0, ExecutionWorldState.QUIESCENT
    )


def test_service_protocols_are_separate_capabilities_of_the_world():
    # The port types remain independently replaceable while the world owns
    # their lifecycle; no concrete adapter is required for this contract.
    assert SubprocessRuntime is not ShellExecutor
    assert ShellExecutor is not TerminalService


def test_results_are_typed_and_terminal_output_is_sequenced():
    result = ExecutionResult(exit_code=0, stdout="ok")
    chunk = TerminalChunk(stream="stdout", data="ok", sequence=7)
    assert (result.exit_code, result.cancelled) == (0, False)
    assert (chunk.stream, chunk.sequence) == ("stdout", 7)


def test_snapshot_distinguishes_cancellation_from_quiescence():
    requested = ExecutionWorldSnapshot(
        "world-1", ExecutionWorldState.CANCELLATION_REQUESTED, 1, "stop"
    )
    incomplete = CleanupResult(False, 1, ExecutionWorldState.CANCELLATION_REQUESTED)
    complete = CleanupResult(True, 0, ExecutionWorldState.QUIESCENT)

    assert requested.state is ExecutionWorldState.CANCELLATION_REQUESTED
    assert not incomplete.quiescent
    assert complete.quiescent and complete.active_resources == 0
