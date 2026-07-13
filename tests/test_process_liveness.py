import errno
import os
import subprocess
import sys
from decimal import Decimal

import pytest

import process_liveness


def _linux_stat(pid=321, comm="worker", state="S", starttime="4242"):
    # Fields after comm begin at proc field 3 (state); starttime is field 22.
    fields = [state] + ["0"] * 18 + [str(starttime)]
    return "%d (%s) %s" % (pid, comm, " ".join(fields))


def test_pid_alive_rejects_invalid_values():
    for value in (
        None, "", "not-a-pid", False, True, 1.0, 1.5,
        Decimal("42"), Decimal("1.5"), Decimal("Infinity"),
        0, -1, 2**32,
    ):
        assert process_liveness.pid_alive(value) is False
        assert process_liveness.probe_process(value) == (
            process_liveness.PROCESS_DEAD,
            None,
        )


@pytest.mark.parametrize(
    ("state", "identity"),
    (
        (process_liveness.PROCESS_ALIVE, "linux:boot:1"),
        (process_liveness.PROCESS_DEAD, None),
        (process_liveness.PROCESS_UNKNOWN, None),
    ),
)
def test_probe_process_preserves_tri_state(monkeypatch, state, identity):
    monkeypatch.setattr(process_liveness, "_platform_family", lambda: "linux")
    monkeypatch.setattr(
        process_liveness,
        "_linux_probe_process",
        lambda pid: (state, identity),
    )

    assert process_liveness.probe_process(123) == (state, identity)


def test_probe_process_expected_identity_match_and_reused_pid(monkeypatch):
    actual = "linux:boot-id:1234"
    monkeypatch.setattr(process_liveness, "_platform_family", lambda: "linux")
    monkeypatch.setattr(
        process_liveness,
        "_linux_probe_process",
        lambda pid: (process_liveness.PROCESS_ALIVE, actual),
    )

    assert process_liveness.probe_process(123, expected_identity=actual) == (
        process_liveness.PROCESS_ALIVE,
        actual,
    )
    assert process_liveness.probe_process(
        123, expected_identity="linux:boot-id:older",
    ) == (process_liveness.PROCESS_DEAD, actual)


def test_probe_process_cannot_confirm_expected_owner_without_identity(monkeypatch):
    monkeypatch.setattr(process_liveness, "_platform_family", lambda: "linux")
    monkeypatch.setattr(
        process_liveness,
        "_linux_probe_process",
        lambda pid: (process_liveness.PROCESS_ALIVE, None),
    )

    assert process_liveness.probe_process(
        123, expected_identity="linux:boot-id:1234",
    ) == (process_liveness.PROCESS_UNKNOWN, None)


def test_pid_alive_treats_unknown_as_alive(monkeypatch):
    monkeypatch.setattr(
        process_liveness,
        "probe_process",
        lambda pid: (process_liveness.PROCESS_UNKNOWN, None),
    )

    assert process_liveness.pid_alive(123) is True


@pytest.mark.parametrize(
    ("native_result", "expected"),
    (
        (
            (process_liveness._WINDOWS_RUNNING, 987654321),
            (process_liveness.PROCESS_ALIVE, "windows:987654321"),
        ),
        (
            (process_liveness._WINDOWS_TERMINATED, None),
            (process_liveness.PROCESS_DEAD, None),
        ),
        (
            (process_liveness._WINDOWS_ACCESS_DENIED, None),
            (process_liveness.PROCESS_UNKNOWN, None),
        ),
        (
            (process_liveness._WINDOWS_OPEN_FAILED, None),
            (process_liveness.PROCESS_UNKNOWN, None),
        ),
        (
            (process_liveness._WINDOWS_WAIT_FAILED, None),
            (process_liveness.PROCESS_UNKNOWN, None),
        ),
        (
            (process_liveness._WINDOWS_TIMES_FAILED, None),
            (process_liveness.PROCESS_UNKNOWN, None),
        ),
        (
            (process_liveness._WINDOWS_API_UNAVAILABLE, None),
            (process_liveness.PROCESS_UNKNOWN, None),
        ),
        (
            (process_liveness._WINDOWS_RUNNING, None),
            (process_liveness.PROCESS_UNKNOWN, None),
        ),
    ),
)
def test_windows_probe_classifies_native_results(
    monkeypatch, native_result, expected,
):
    monkeypatch.setattr(
        process_liveness,
        "_windows_native_process_probe",
        lambda pid: native_result,
    )

    assert process_liveness._windows_probe_process(123) == expected


def test_parse_linux_stat_handles_spaces_and_closing_parenthesis_in_comm():
    raw = _linux_stat(comm="worker ) pool", state="R", starttime="98765")

    assert process_liveness._parse_linux_stat(raw, expected_pid=321) == {
        "pid": 321,
        "state": "R",
        "starttime": "98765",
    }
    assert process_liveness._parse_linux_stat(raw, expected_pid=999) is None


def test_linux_probe_uses_boot_id_and_starttime(monkeypatch):
    monkeypatch.setattr(
        process_liveness, "_read_linux_stat", lambda pid: _linux_stat(pid=pid),
    )
    monkeypatch.setattr(
        process_liveness, "_read_linux_boot_id", lambda: "boot-id",
    )

    assert process_liveness._linux_probe_process(321) == (
        process_liveness.PROCESS_ALIVE,
        "linux:boot-id:4242",
    )


def test_linux_probe_treats_zombie_as_dead(monkeypatch):
    monkeypatch.setattr(
        process_liveness,
        "_read_linux_stat",
        lambda pid: _linux_stat(pid=pid, state="Z"),
    )
    monkeypatch.setattr(
        process_liveness,
        "_read_linux_boot_id",
        lambda: pytest.fail("zombie identity should not be read"),
    )

    assert process_liveness._linux_probe_process(321) == (
        process_liveness.PROCESS_DEAD,
        None,
    )


@pytest.mark.parametrize(
    ("error", "state"),
    (
        (FileNotFoundError(), process_liveness.PROCESS_DEAD),
        (PermissionError(), process_liveness.PROCESS_UNKNOWN),
        (OSError(errno.EIO, "probe failed"), process_liveness.PROCESS_UNKNOWN),
    ),
)
def test_linux_probe_classifies_proc_failures(monkeypatch, error, state):
    def fail(_pid):
        raise error

    monkeypatch.setattr(process_liveness, "_read_linux_stat", fail)

    assert process_liveness._linux_probe_process(321) == (state, None)


@pytest.mark.parametrize(
    ("result", "expected"),
    (
        (
            subprocess.CompletedProcess(
                [], 0, "S Mon Jul 13 01:02:03 2026\n", "",
            ),
            (
                process_liveness.PROCESS_ALIVE,
                "posix:Mon Jul 13 01:02:03 2026",
            ),
        ),
        (
            subprocess.CompletedProcess(
                [], 0, "Z Mon Jul 13 01:02:03 2026\n", "",
            ),
            (process_liveness.PROCESS_DEAD, None),
        ),
        (
            subprocess.CompletedProcess([], 1, "", ""),
            (process_liveness.PROCESS_DEAD, None),
        ),
        (
            subprocess.CompletedProcess([], 2, "", "ps failed"),
            (process_liveness.PROCESS_UNKNOWN, None),
        ),
    ),
)
def test_posix_ps_fallback_is_conservative(monkeypatch, result, expected):
    monkeypatch.setattr(process_liveness, "_run_ps", lambda pid: result)

    assert process_liveness._posix_probe_process(321) == expected


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
