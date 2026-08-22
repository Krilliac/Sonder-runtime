from __future__ import annotations

from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.jobs.durable_registry import ProcessTreeCleanupRequest


def _request(*, group: int | None = 22) -> ProcessTreeCleanupRequest:
    return ProcessTreeCleanupRequest("job-1", 11, group, max_descendants=8)


def test_posix_requires_process_group_to_prove_tree_cleanup() -> None:
    supervisor = ProcessTreeSupervisor(platform_name="posix")
    receipt = supervisor.cleanup(_request(group=None))
    assert receipt.requested is False
    assert receipt.complete is False
    assert "process_group_id" in receipt.detail


def test_posix_kills_the_owned_process_group() -> None:
    calls: list[tuple[int, int]] = []
    fake_os = SimpleNamespace(name="posix", killpg=lambda group, sig: calls.append((group, sig)))
    fake_signal = SimpleNamespace(SIGKILL=9)
    supervisor = ProcessTreeSupervisor(os_module=fake_os, signal_module=fake_signal, platform_name="posix")

    receipt = supervisor.cleanup(_request())

    assert calls == [(22, 9)]
    assert receipt.requested and receipt.complete


def test_posix_treats_an_already_exited_group_as_complete() -> None:
    def killpg(_group: int, _signal: int) -> None:
        raise ProcessLookupError

    fake_os = SimpleNamespace(name="posix", killpg=killpg)
    supervisor = ProcessTreeSupervisor(os_module=fake_os, signal_module=SimpleNamespace(SIGKILL=9), platform_name="posix")
    receipt = supervisor.cleanup(_request())
    assert receipt.complete
    assert "already exited" in receipt.detail


def test_windows_uses_os_tree_operation_and_reports_failure() -> None:
    calls: list[list[str]] = []

    class FakeSubprocess:
        DEVNULL = object()
        SubprocessError = RuntimeError

        @staticmethod
        def run(argv, **kwargs):
            calls.append(argv)
            assert kwargs["shell"] is False
            return SimpleNamespace(returncode=1)

    supervisor = ProcessTreeSupervisor(
        os_module=SimpleNamespace(name="nt"),
        subprocess_module=FakeSubprocess,
        platform_name="nt",
    )
    receipt = supervisor.cleanup(_request())
    assert calls == [["taskkill", "/PID", "11", "/T", "/F"]]
    assert receipt.requested and not receipt.complete


def test_unsupported_platform_fails_closed() -> None:
    receipt = ProcessTreeSupervisor(platform_name="other").cleanup(_request())
    assert not receipt.requested
    assert not receipt.complete


def test_request_type_is_checked_before_platform_side_effects() -> None:
    with pytest.raises(TypeError):
        ProcessTreeSupervisor(platform_name="posix").cleanup(object())  # type: ignore[arg-type]
