"""Phase 0 (D8 host backend): opt-in run limits for run_code.

limits=None keeps today's behaviour exactly. With RunLimits the child runs
in a Job Object with a process cap and a job memory cap (fail closed if they
cannot be set), under RLIMIT_NPROC/AS/FSIZE on POSIX, Python with -I, and
with bounded output that kills the child past a total byte threshold.
"""
import dataclasses
import os
import sys

import pytest

import code_runner

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object limits")
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX resource limits")


def test_run_limits_defaults_match_the_design():
    limits = code_runner.RunLimits()
    assert limits.process_limit == 16
    assert limits.job_memory_bytes == 512 * 1024 * 1024
    assert limits.isolated_python is True
    assert limits.capture_bytes_per_stream == 64 * 1024
    assert limits.kill_after_total_bytes == 8 * 1024 * 1024
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.process_limit = 99
    with pytest.raises(ValueError):
        code_runner.RunLimits(process_limit=0)


def _capture_cmd(monkeypatch):
    seen = {}

    def fake_run_process(cmd, cwd, stdin, timeout, language, limits=None):
        seen["cmd"] = cmd
        seen["limits"] = limits
        return code_runner._error_result(language, cwd, timeout, "")

    monkeypatch.setattr(code_runner, "_run_process", fake_run_process)
    return seen


def test_python_runs_isolated_only_with_limits(monkeypatch):
    seen = _capture_cmd(monkeypatch)

    code_runner.run_code("print(1)", limits=code_runner.RunLimits())
    assert seen["cmd"][:4] == [sys.executable, "-I", "-X", "utf8"]
    assert isinstance(seen["limits"], code_runner.RunLimits)

    code_runner.run_code("print(1)")
    assert "-I" not in seen["cmd"]
    assert seen["limits"] is None


def test_limited_run_preserves_utf8_output():
    out = code_runner.run_code(
        'import sys\nprint("🎲 café", sys.flags.isolated)\n',
        limits=code_runner.RunLimits(),
    )
    assert out["ok"] is True, out
    assert "🎲 café 1" in out["stdout"]


def test_isolation_can_be_turned_off_explicitly(monkeypatch):
    seen = _capture_cmd(monkeypatch)
    code_runner.run_code("print(1)", limits=code_runner.RunLimits(isolated_python=False))
    assert "-I" not in seen["cmd"]


def test_output_flood_past_the_kill_threshold_kills_the_child(tmp_path):
    limits = code_runner.RunLimits(kill_after_total_bytes=1024 * 1024)
    source = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n"

    out = code_runner.run_code(source, timeout=30, limits=limits)

    assert out["ok"] is False
    assert "output exceeded" in out["error"]
    assert "killed" in out["error"]
    assert len(out["stdout"]) <= code_runner.MAX_OUTPUT_CHARS


def test_limited_run_still_returns_normal_output():
    out = code_runner.run_code(
        "import sys\nprint('hello', sys.flags.isolated)\n", limits=code_runner.RunLimits(),
    )
    assert out["ok"] is True, out
    assert out["stdout"].strip() == "hello 1"


@windows_only
def test_process_limit_stops_a_fork_flood():
    source = (
        "import subprocess, sys\n"
        "started = 0\n"
        "for _ in range(40):\n"
        "    try:\n"
        "        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],\n"
        "                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "                         stderr=subprocess.DEVNULL)\n"
        "    except OSError:\n"
        "        break\n"
        "    started += 1\n"
        "print('started', started)\n"
    )
    out = code_runner.run_code(source, timeout=60, limits=code_runner.RunLimits())

    assert out["ok"] is True, out
    started = int(out["stdout"].split()[-1])
    assert started < 16


@windows_only
def test_job_memory_limit_stops_a_one_gib_allocation():
    out = code_runner.run_code(
        "data = bytearray(1024 ** 3)\nprint('allocated', len(data))\n",
        timeout=60, limits=code_runner.RunLimits(),
    )
    assert out["ok"] is False
    assert "allocated" not in out["stdout"]
    assert "MemoryError" in out["stderr"]


@windows_only
def test_without_limits_the_same_allocation_is_not_capped():
    # Control for the test above: the cap comes from RunLimits, not the host.
    out = code_runner.run_code(
        "data = bytearray(1024 ** 3)\nprint('allocated', len(data))\n", timeout=60,
    )
    assert out["ok"] is True, out


def test_limits_fail_closed_when_the_job_cannot_be_configured(monkeypatch, tmp_path):
    events = []

    class Proc:
        pid = 99
        returncode = None
        stdin = stdout = stderr = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            events.append("wait")
            self.returncode = 1
            return 1

        def kill(self):
            events.append("kill")

    def fake_popen(cmd, **kwargs):
        events.append(("spawn", kwargs.get("creationflags", 0)))
        return Proc()

    monkeypatch.setattr(code_runner.os, "name", "nt", raising=False)
    monkeypatch.setattr(code_runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(code_runner, "_attach_windows_job", lambda proc, **kwargs: None)
    monkeypatch.setattr(code_runner, "_terminate_process_tree", lambda proc: events.append("terminate"))
    monkeypatch.setattr(
        code_runner, "_resume_windows_process", lambda proc: events.append("resume"),
    )

    out = code_runner._run_process(
        [sys.executable, "-c", "pass"], str(tmp_path), "", 10, "python",
        limits=code_runner.RunLimits(),
    )

    assert out["ok"] is False
    assert "limits" in out["error"]
    spawn = events[0]
    assert spawn[0] == "spawn" and spawn[1] & 0x00000004  # CREATE_SUSPENDED
    assert "resume" not in events
    assert "terminate" in events


def test_windows_job_requests_process_and_memory_limits(monkeypatch):
    seen = {}

    def fake_attach(proc, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(code_runner, "_attach_windows_job", fake_attach)
    code_runner._limited_windows_job(object(), code_runner.RunLimits())
    assert seen == {"active_process_limit": 16, "job_memory_bytes": 512 * 1024 * 1024}


class _FakeResource:
    RLIMIT_NPROC = 6
    RLIMIT_AS = 9
    RLIMIT_FSIZE = 1
    RLIM_INFINITY = -1

    def __init__(self, hard):
        self.hard = hard
        self.calls = []

    def getrlimit(self, which):
        return (self.hard.get(which, -1), self.hard.get(which, -1))

    def setrlimit(self, which, pair):
        self.calls.append((which, pair))


def test_posix_preexec_sets_nproc_as_and_fsize():
    fake = _FakeResource({})
    limits = code_runner.RunLimits()
    apply = code_runner._posix_limit_preexec(limits, resource_module=fake)

    assert fake.calls == []  # nothing happens until the child runs it
    apply()

    assert fake.calls == [
        (fake.RLIMIT_NPROC, (16, 16)),
        (fake.RLIMIT_AS, (limits.job_memory_bytes,) * 2),
        (fake.RLIMIT_FSIZE, (limits.file_size_bytes,) * 2),
    ]


def test_posix_preexec_never_raises_a_lower_hard_limit():
    fake = _FakeResource({_FakeResource.RLIMIT_NPROC: 8})
    code_runner._posix_limit_preexec(code_runner.RunLimits(), resource_module=fake)()
    assert fake.calls[0] == (fake.RLIMIT_NPROC, (8, 8))


def test_posix_preexec_fails_closed_without_the_resource():
    class Missing(_FakeResource):
        RLIMIT_NPROC = None

    with pytest.raises(OSError, match="RLIMIT_NPROC"):
        code_runner._posix_limit_preexec(code_runner.RunLimits(), resource_module=Missing({}))


@posix_only
def test_posix_child_runs_under_the_limits():
    source = (
        "import resource\n"
        "print(*(resource.getrlimit(getattr(resource, n))[0]\n"
        "        for n in ('RLIMIT_NPROC', 'RLIMIT_AS', 'RLIMIT_FSIZE')))\n"
    )
    limits = code_runner.RunLimits()
    out = code_runner.run_code(source, limits=limits)

    assert out["ok"] is True, out
    nproc, address_space, file_size = (int(v) for v in out["stdout"].split())
    assert nproc <= limits.process_limit
    assert address_space <= limits.job_memory_bytes
    assert file_size <= limits.file_size_bytes
