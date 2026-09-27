"""Deterministic OPS-005 group-absence contract regressions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.jobs.durable_registry import ProcessTreeCleanupRequest


def _request(*, identity="owner", group=42):
    return ProcessTreeCleanupRequest(
        "group-job", 42, group, process_identity=identity,
    )


def test_dead_root_with_absent_group_can_complete_without_sigkill():
    calls = []

    def killpg(group, sig):
        calls.append((group, sig))
        if sig == 0:
            raise ProcessLookupError

    supervisor = ProcessTreeSupervisor(
        os_module=SimpleNamespace(name="posix", killpg=killpg, getpgid=lambda pid: 42),
        signal_module=SimpleNamespace(SIGKILL=9),
        platform_name="posix",
        process_probe=lambda *_args: ("dead", None),
    )
    receipt = supervisor.cleanup(_request())
    assert receipt.complete is True
    assert calls == [(42, 0)]


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_dead_root_group_probe_error_is_incomplete(error):
    calls = []

    def killpg(group, sig):
        calls.append((group, sig))
        if sig == 0:
            raise error("group probe failed")

    supervisor = ProcessTreeSupervisor(
        os_module=SimpleNamespace(name="posix", killpg=killpg, getpgid=lambda pid: 42),
        signal_module=SimpleNamespace(SIGKILL=9),
        platform_name="posix",
        process_probe=lambda *_args: ("dead", None),
    )
    receipt = supervisor.cleanup(_request())
    assert receipt.complete is False
    assert calls == [(42, 0)]


def test_unknown_root_refuses_before_group_probe():
    calls = []
    supervisor = ProcessTreeSupervisor(
        os_module=SimpleNamespace(name="posix", killpg=lambda *args: calls.append(args)),
        signal_module=SimpleNamespace(SIGKILL=9), platform_name="posix",
        process_probe=lambda *_args: ("unknown", None),
    )
    receipt = supervisor.cleanup(_request())
    assert receipt.complete is False and calls == []


@pytest.mark.parametrize("state", ("alive", "dead"))
def test_reused_pid_with_wrong_identity_refuses_without_group_query_or_kill(state):
    calls = []
    supervisor = ProcessTreeSupervisor(
        os_module=SimpleNamespace(name="posix", killpg=lambda *args: calls.append(args)),
        signal_module=SimpleNamespace(SIGKILL=9),
        platform_name="posix",
        process_probe=lambda *_args: (state, "replacement"),
    )
    receipt = supervisor.cleanup(_request())
    assert receipt.complete is False
    assert calls == []


def test_dead_root_with_group_different_from_pid_refuses_before_probe():
    calls = []
    supervisor = ProcessTreeSupervisor(
        os_module=SimpleNamespace(name="posix", killpg=lambda *args: calls.append(args), getpgid=lambda pid: 77),
        signal_module=SimpleNamespace(SIGKILL=9), platform_name="posix",
        process_probe=lambda *_args: ("dead", None),
    )
    receipt = supervisor.cleanup(_request(group=43))
    assert receipt.complete is False and calls == []


def test_dead_root_with_missing_group_refuses_before_probe():
    supervisor = ProcessTreeSupervisor(
        os_module=SimpleNamespace(name="posix", killpg=lambda *args: (_ for _ in ()).throw(AssertionError(args))),
        signal_module=SimpleNamespace(SIGKILL=9), platform_name="posix",
        process_probe=lambda *_args: ("dead", None),
    )
    receipt = supervisor.cleanup(_request(group=None))
    assert receipt.complete is False


def test_missing_group_refuses_without_identity_or_kill_probe():
    calls = []
    supervisor = ProcessTreeSupervisor(
        os_module=SimpleNamespace(name="posix", killpg=lambda *args: calls.append(args)),
        platform_name="posix",
        process_probe=lambda *_args: ("alive", "owner"),
    )
    receipt = supervisor.cleanup(_request(group=None))
    assert receipt.complete is False
    assert calls == []


def test_identity_none_preserves_legacy_request_shape_only():
    calls = []
    supervisor = ProcessTreeSupervisor(
        os_module=SimpleNamespace(name="posix", killpg=lambda *args: calls.append(args)),
        signal_module=SimpleNamespace(SIGKILL=9), platform_name="posix",
    )
    receipt = supervisor.cleanup(_request(identity=None))
    assert receipt.requested is True
    assert calls == [(42, 9)]
