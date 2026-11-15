"""Process-tree adapter: a child that spawns a grandchild must not outlive
timeout, cancellation or its own exit, on the OS running the test.

Linux-only assertions (``/proc`` based reaping and subreaper behaviour) are
skipped elsewhere and run in the Linux CI job.
"""
from __future__ import annotations

import os
import sys
import textwrap
import threading
import time

import pytest

from sonder_runtime.adapters import process_liveness, process_tree
from sonder_runtime.platform.process import CancellationToken

_IS_LINUX = sys.platform.startswith("linux")
_IS_POSIX = os.name == "posix"

# The child starts a grandchild that sleeps, publishes the grandchild pid, then
# either sleeps itself or exits, depending on argv[2].
_CHILD = textwrap.dedent(
    """
    import signal, subprocess, sys, time
    pid_file, mode = sys.argv[1], sys.argv[2]
    if mode == "ignore-term" and hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    grandchild_code = (
        "import signal, time\\n"
        "if %r == 'ignore-term' and hasattr(signal, 'SIGTERM'):\\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "time.sleep(120)\\n" % mode
    )
    grandchild = subprocess.Popen([sys.executable, "-c", grandchild_code])
    with open(pid_file, "w") as handle:
        handle.write(str(grandchild.pid))
    if mode == "exit-early":
        sys.exit(0)
    time.sleep(120)
    """
)


def _spawn(tmp_path, mode="sleep"):
    pid_file = tmp_path / "grandchild.pid"
    tree = process_tree.ProcessTree.start(
        [sys.executable, "-c", _CHILD, str(pid_file), mode],
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return tree, int(pid_file.read_text().strip())
        time.sleep(0.05)
    tree.terminate()
    pytest.fail("grandchild pid was never published")


def _gone(pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_liveness.pid_alive(pid):
            return True
        time.sleep(0.05)
    return not process_liveness.pid_alive(pid)


def test_timeout_removes_child_and_grandchild(tmp_path):
    tree, grandchild = _spawn(tmp_path)
    child = tree.pid
    try:
        result = tree.wait(timeout=0.3, grace_seconds=0.5)
    finally:
        tree.close()
    assert result.state is process_tree.TreeState.TIMED_OUT
    assert result.complete is True
    assert result.returncode is not None
    assert _gone(child)
    assert _gone(grandchild)


def test_cancellation_removes_child_and_grandchild(tmp_path):
    tree, grandchild = _spawn(tmp_path)
    child = tree.pid
    token = CancellationToken()
    threading.Timer(0.2, token.cancel).start()
    try:
        result = tree.wait(timeout=60, cancel=token, grace_seconds=0.5)
    finally:
        tree.close()
    assert result.state is process_tree.TreeState.CANCELLED
    assert result.complete is True
    assert _gone(child)
    assert _gone(grandchild)


def test_grandchild_left_behind_by_an_exited_child_is_swept(tmp_path):
    tree, grandchild = _spawn(tmp_path, mode="exit-early")
    try:
        result = tree.wait(timeout=30, grace_seconds=0.5)
    finally:
        tree.close()
    assert result.state is process_tree.TreeState.EXITED
    assert result.returncode == 0
    assert result.swept is True
    assert result.complete is True
    assert _gone(grandchild)


def test_sweep_can_be_declined_for_a_child_that_daemonises_on_purpose(tmp_path):
    tree, grandchild = _spawn(tmp_path, mode="exit-early")
    try:
        result = tree.wait(timeout=30, sweep_on_exit=False)
        assert result.state is process_tree.TreeState.EXITED
        assert result.swept is False
        assert process_liveness.pid_alive(grandchild)
    finally:
        tree.terminate(grace_seconds=0.5)
        tree.close()
    assert _gone(grandchild)


def test_release_leaves_a_detached_survivor_running(tmp_path):
    tree, grandchild = _spawn(tmp_path, mode="exit-early")
    try:
        result = tree.wait(timeout=30, sweep_on_exit=False)
        assert result.state is process_tree.TreeState.EXITED
        tree.release()
        time.sleep(0.3)
        assert process_liveness.pid_alive(grandchild), "release must not kill survivors"
    finally:
        _force_kill(grandchild)


def _force_kill(pid):
    """Test cleanup for a survivor the test deliberately kept alive."""
    import subprocess

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False,
        )
        return
    import signal

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass

def test_clean_exit_reports_exit_without_force(tmp_path):
    tree = process_tree.ProcessTree.start([sys.executable, "-c", "raise SystemExit(3)"])
    try:
        result = tree.wait(timeout=30)
    finally:
        tree.close()
    assert result.state is process_tree.TreeState.EXITED
    assert result.returncode == 3
    assert result.escalated is False
    assert result.swept is False
    assert result.complete is True


def test_terminate_is_idempotent(tmp_path):
    tree, grandchild = _spawn(tmp_path)
    try:
        first = tree.terminate(grace_seconds=0.5)
        second = tree.terminate(grace_seconds=0.5)
    finally:
        tree.close()
    assert first.complete is True
    assert second.complete is True
    assert _gone(grandchild)


def test_start_failure_is_the_launchers_oserror(tmp_path):
    with pytest.raises(OSError):
        process_tree.ProcessTree.start([str(tmp_path / "no-such-binary")])


def test_start_requires_a_non_empty_argv():
    with pytest.raises(ValueError):
        process_tree.ProcessTree.start([])


@pytest.mark.skipif(not _IS_POSIX, reason="SIGTERM-then-SIGKILL escalation is POSIX only")
def test_term_ignoring_tree_is_escalated_to_kill(tmp_path):
    tree, grandchild = _spawn(tmp_path, mode="ignore-term")
    child = tree.pid
    try:
        result = tree.wait(timeout=0.3, grace_seconds=0.4)
    finally:
        tree.close()
    assert result.state is process_tree.TreeState.TIMED_OUT
    assert result.escalated is True
    assert result.complete is True
    assert _gone(child)
    assert _gone(grandchild)


@pytest.mark.skipif(not _IS_LINUX, reason="/proc reaping assertions are Linux only")
def test_linux_root_is_reaped_not_left_as_a_zombie(tmp_path):
    tree, grandchild = _spawn(tmp_path)
    child = tree.pid
    try:
        result = tree.wait(timeout=0.3, grace_seconds=0.5)
    finally:
        tree.close()
    assert result.complete is True
    # A reaped root has no /proc entry at all; an unreaped one lingers as "Z".
    assert not os.path.exists("/proc/%d" % child)
    assert _gone(grandchild)


@pytest.mark.skipif(not _IS_LINUX, reason="/proc reaping assertions are Linux only")
def test_linux_no_live_member_remains_in_the_process_group(tmp_path):
    tree, _grandchild = _spawn(tmp_path)
    pgid = tree.pid
    try:
        tree.wait(timeout=0.3, grace_seconds=0.5)
    finally:
        tree.close()
    assert process_tree.live_group_members(pgid) == ()


@pytest.mark.skipif(not _IS_LINUX, reason="subreaper reaping is Linux only")
def test_linux_orphans_adopted_by_a_subreaper_are_reaped(tmp_path):
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    pr_get = 37  # PR_GET_CHILD_SUBREAPER
    pr_set = 36  # PR_SET_CHILD_SUBREAPER
    before = ctypes.c_int(0)
    libc.prctl(pr_get, ctypes.byref(before), 0, 0, 0)
    if libc.prctl(pr_set, 1, 0, 0, 0) != 0:
        pytest.skip("cannot become a child subreaper here")
    try:
        tree, grandchild = _spawn(tmp_path, mode="exit-early")
        try:
            result = tree.wait(timeout=30, grace_seconds=0.5)
        finally:
            tree.close()
        assert result.swept is True
        assert result.complete is True
        # The killed grandchild was re-parented to us; it must be reaped, so
        # its /proc entry is gone rather than a lingering zombie.
        deadline = time.monotonic() + 5
        while os.path.exists("/proc/%d" % grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not os.path.exists("/proc/%d" % grandchild)
    finally:
        libc.prctl(pr_set, int(before.value), 0, 0, 0)
