"""The packaged bounded runner: output limit, tree kill on timeout, start failure."""
import os
import sys
import time

import pytest

from sonder_runtime.adapters.host_tools import bounded_process
from sonder_runtime.platform import runtime_threads

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); treat a reaped/zombie state as dead.
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split()[2] != "Z"
    except OSError:
        return False


def test_ok_run_returns_output_and_exit_code():
    result = bounded_process.run_bounded([sys.executable, "-c", "print('hello 1.2.3')"], timeout_seconds=20)
    assert result.outcome == "ok" and result.exit_code == 0
    assert result.output.strip() == "hello 1.2.3"


def test_nonzero_exit_is_error():
    result = bounded_process.run_bounded([sys.executable, "-c", "raise SystemExit(3)"], timeout_seconds=20)
    assert result.outcome == "error" and result.exit_code == 3


def test_output_limit_kills_the_producer():
    started = time.monotonic()
    result = bounded_process.run_bounded(
        [sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('x' * 4096)"],
        timeout_seconds=20, max_output_chars=2_000,
    )
    assert result.outcome == "output_limit"
    assert len(result.output) == 2_000
    assert time.monotonic() - started < 15


@posix_only
def test_timeout_kills_the_whole_process_tree(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    started = time.monotonic()
    result = bounded_process.run_bounded([sys.executable, "-c", script], timeout_seconds=2)
    assert result.outcome == "timeout"
    assert time.monotonic() - started < 15
    deadline = time.monotonic() + 10
    grandchild = int(pid_file.read_text())
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild), "grandchild survived the process-tree kill"


def test_missing_binary_is_start_failed(tmp_path):
    result = bounded_process.run_bounded([str(tmp_path / "does-not-exist")], timeout_seconds=5)
    assert result.outcome == "start_failed" and result.exit_code is None


def test_drain_threads_come_from_runtime_threads(monkeypatch):
    created = []
    native = runtime_threads.Thread

    def recording_thread(*args, **kwargs):
        thread = native(*args, **kwargs)
        created.append(kwargs.get("name"))
        return thread

    monkeypatch.setattr(runtime_threads, "Thread", recording_thread)
    result = bounded_process.run_bounded([sys.executable, "-c", "print(1)"], timeout_seconds=20)
    assert result.outcome == "ok"
    assert created == ["sonder-bounded-probe-drain", "sonder-bounded-probe-drain"]


def test_argv_validation_rejects_nul_and_empty():
    with pytest.raises(ValueError):
        bounded_process.run_bounded([])
    with pytest.raises(ValueError):
        bounded_process.run_bounded(["a\x00b"])


def test_legacy_toolchain_wrapper_delegates_to_the_packaged_runner(monkeypatch):
    import toolchain_status

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen.update(kwargs)
        return bounded_process.BoundedRun("ok", "cargo 9.9.9", 0, 1)

    monkeypatch.setattr(bounded_process, "run_bounded", fake_run)
    monkeypatch.setattr(toolchain_status, "TIMEOUT_SECONDS", 7)
    assert toolchain_status._run_bounded(["cargo", "--version"]) == ("ok", "cargo 9.9.9")
    assert seen["timeout_seconds"] == 7
    assert seen["max_output_chars"] == toolchain_status.MAX_OUTPUT_CHARS
    assert seen["subprocess_module"] is toolchain_status.subprocess


def test_legacy_wrapper_maps_start_failure_to_oserror(monkeypatch):
    import toolchain_status

    monkeypatch.setattr(bounded_process, "run_bounded",
                        lambda argv, **kwargs: bounded_process.BoundedRun("start_failed", "", None, 0))
    with pytest.raises(OSError):
        toolchain_status._run_bounded(["missing"])
