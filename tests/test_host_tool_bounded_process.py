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


def test_run_starts_in_a_neutral_directory_not_the_server_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = bounded_process.run_bounded(
        (sys.executable, "-c", "import os; print(os.getcwd())"), timeout_seconds=20,
    )
    assert result.outcome == "ok"
    reported = result.output.strip()
    assert os.path.realpath(reported) != os.path.realpath(str(tmp_path))
    assert os.path.realpath(reported) == os.path.realpath(bounded_process.neutral_cwd())


def test_explicit_cwd_is_honoured(tmp_path):
    result = bounded_process.run_bounded(
        (sys.executable, "-c", "import os; print(os.getcwd())"), timeout_seconds=20,
        cwd=str(tmp_path),
    )
    assert os.path.realpath(result.output.strip()) == os.path.realpath(str(tmp_path))


def test_which_in_directory_never_consults_the_current_directory(tmp_path, monkeypatch):
    from sonder_runtime.adapters.host_tools.probes import which_in_directory

    planted = tmp_path / "cwd"
    planted.mkdir()
    for name in ("tool.exe", "tool"):
        (planted / name).write_text("x")
        (planted / name).chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATHEXT", ".JS;.EXE")
    assert which_in_directory("tool", str(empty), windows=True) is None
    assert which_in_directory("tool", str(empty), windows=False) is None
    host = tmp_path / "host"
    host.mkdir()
    (host / "tool.js").write_text("x")
    assert which_in_directory("tool", str(host), windows=True) is None  # PATHEXT ignored
    (host / "tool.cmd").write_text("x")
    assert which_in_directory("tool", str(host), windows=True) == str(host / "tool.cmd")
    assert which_in_directory("tool", "relative", windows=True) is None
    assert which_in_directory("../tool", str(host), windows=True) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute bit")
def test_which_in_directory_requires_the_execute_bit_on_posix(tmp_path):
    from sonder_runtime.adapters.host_tools.probes import which_in_directory

    (tmp_path / "tool").write_text("x")
    (tmp_path / "tool").chmod(0o644)
    assert which_in_directory("tool", str(tmp_path)) is None
    (tmp_path / "tool").chmod(0o755)
    assert which_in_directory("tool", str(tmp_path)) == str(tmp_path / "tool")
