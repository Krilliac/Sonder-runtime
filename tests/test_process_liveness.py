import os
import subprocess
import sys
from decimal import Decimal

import pytest

import process_liveness


def test_pid_alive_rejects_invalid_values():
    for value in (
        None, "", "not-a-pid", False, True, 1.0, 1.5,
        Decimal("42"), Decimal("1.5"), Decimal("Infinity"),
        0, -1, 2**32,
    ):
        assert process_liveness.pid_alive(value) is False


def test_pid_alive_recognizes_current_process():
    assert process_liveness.pid_alive(os.getpid()) is True


def test_pid_alive_tracks_child_lifecycle():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process_liveness.pid_alive(child.pid) is True
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert process_liveness.pid_alive(child.pid) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows exit-code regression")
def test_pid_alive_does_not_confuse_exit_code_259_with_still_running():
    child = subprocess.Popen(
        [sys.executable, "-c", "raise SystemExit(259)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert child.wait(timeout=10) == 259
    assert process_liveness.pid_alive(child.pid) is False
