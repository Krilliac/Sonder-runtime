import os
import platform
import subprocess
import sys

from sonder_runtime.application.owner_process import process_is_alive, recorded_owner_is_dead


def test_windows_owner_probe_reports_current_process_live():
    assert process_is_alive(os.getpid(), platform.node()) is True


def test_windows_owner_probe_reports_terminated_process_dead():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)
    assert process_is_alive(process.pid, platform.node()) is False
    assert recorded_owner_is_dead({"owner_pid": str(process.pid), "owner_host": platform.node()})


def test_owner_probe_fails_closed_for_unknown_host_and_malformed_pid():
    assert process_is_alive(1, "host-that-cannot-own-this-reservation") is None
    assert not recorded_owner_is_dead({"owner_pid": "not-a-pid", "owner_host": platform.node()})
