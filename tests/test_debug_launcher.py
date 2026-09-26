"""ProcessDebugLauncher over the real durable process provider.

Steps are small Python "debugger" shims launched through the production
SubprocessJobProvider and SQLite job registry, so deadlines, cancellation,
process-tree kill and output retention are the real ones, and so is the
``domain.debugging.templates`` materialization the launcher binds with.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from sonder_runtime.adapters.debugging.capture_source import GuardedCaptureSource
from sonder_runtime.adapters.debugging.launcher import ProcessDebugLauncher
from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.adapters.process_liveness import PROCESS_DEAD, probe_process
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.debugging.ports import DebugPlan, DebugStep

pytestmark = pytest.mark.integration


SHIM = textwrap.dedent('''
    import os, sys, time, subprocess, json
    mode, nonce, staged, rundir = sys.argv[1:5]
    extra = sys.argv[5:]
    print("SONDER_%s_BEGIN" % nonce, flush=True)
    if mode == "echo":
        print(json.dumps({"cwd": os.getcwd(), "env": dict(os.environ), "input": staged,
                          "data": open(staged, "rb").read()[:16].hex(), "rundir": rundir}), flush=True)
    elif mode == "sleep":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(extra[0], "w") as handle:
            handle.write("%d %d" % (os.getpid(), child.pid))
        time.sleep(120)
    elif mode == "flood":
        line = "x" * 4000 + "\\n"
        while True:
            sys.stdout.write(line)
    elif mode == "touch":
        with open(staged, "ab") as handle:
            handle.write(b"changed")
    elif mode == "file":
        os.makedirs(os.path.join(rundir, "out"), exist_ok=True)
        with open(os.path.join(rundir, "out", "cpu.txt"), "w") as handle:
            handle.write("Function,Weight\\nmain,10\\n")
    elif mode == "fail":
        sys.exit(3)
    print("SONDER_%s_END" % nonce, flush=True)
''')


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.allowed = tmp_path / "allowed"
        self.allowed.mkdir()
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.shim = tmp_path / "shim.py"
        self.shim.write_text(SHIM)
        self.registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
        self.provider = SubprocessJobProvider(self.registry, process_cleanup=ProcessTreeSupervisor())
        self.source = GuardedCaptureSource()
        self.guarded: list[str] = []
        self.refuse: set[str] = set()
        self.launcher = self.make_launcher()

    def make_launcher(self, **kwargs):
        def guard(path):
            self.guarded.append(path)
            if path in self.refuse:
                raise PermissionError("host executable rejected")
            return path

        return ProcessDebugLauncher(lambda: self.provider, lambda: self.registry,
                                    executable_guard=guard, run_root=str(self.state / "debug-runs"),
                                    source=self.source, **kwargs)

    def capture(self, name="core.1", data=b"\x7fELF" + b"\x02\x01\x01" + b"\x00" * 9 + b"\x04\x00" * 30):
        path = self.allowed / name
        path.write_bytes(data)
        reader, ident = self.source.open_reader(str(path))
        reader.close()
        return ident

    def step(self, mode, *extra, timeout=60, via="argv", env=()):
        argv = (sys.executable, str(self.shim), mode, "{nonce}", "{input}", "{rundir}", *extra)
        environment = (("PATH", "/usr/bin:/bin"), ("HOME", "{rundir}/home"), ("TMPDIR", "{rundir}/tmp")) + env
        return DebugStep(engine="gdb", template_argv=argv, display_argv=argv, environment=environment,
                         timeout_seconds=timeout, max_output_bytes=16 << 20,
                         memory_limit_bytes=4 << 30, parser="gdb", reads_output_via=via)

    def plan(self, ident, steps, staging="copy", **extra):
        return DebugPlan(kind="crash", source_kind=ident.kind, input_label=ident.label,
                         input_sha256=ident.sha256, input_bytes=ident.size, input_identity=ident,
                         staging=staging, steps=tuple(steps), network=False, stores_display=(),
                         verified_modules=(), checked_executables=(sys.executable,), notes=(),
                         command_digest="c" * 64, egress_isolation="none",
                         engines=("pure", "gdb"), **extra)

    def output(self, job_id):
        page = self.registry.stream(job_id, max_events=256, max_bytes=1 << 20)
        return "".join(event.data for event in page.events)


def context(principal="owner"):
    return replace(local_owner_context(correlation_id=uuid.uuid4().hex), principal_id=principal)


def run_id():
    return "debug-run-" + uuid.uuid4().hex


def _alive(pid: int) -> bool:
    """Not yet proven dead: a zombie reads as dead, an unreadable probe as alive."""
    return probe_process(pid)[0] != PROCESS_DEAD


def _pids(path: Path, limit=30.0) -> list[int]:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return [int(item) for item in path.read_text().split()]
        time.sleep(0.05)
    raise AssertionError("the shim never started")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path / "allowed"))
    monkeypatch.setenv("SONDER_LAUNCHER_LEAK_CHECK", "should-not-leak")
    # SONDER_* names are already scrubbed by child_environment(); these are not,
    # so they prove the step runs with a replacement environment.
    monkeypatch.setenv("DEBUGGER_LEAK_CANARY", "should-not-leak")
    monkeypatch.setenv("GDBHISTFILE", "/tmp/should-not-leak")
    return Env(tmp_path)


def test_a_chain_binds_nonce_rundir_and_input_and_cleans_up(env):
    ident = env.capture()
    plan = env.plan(ident, [env.step("echo"), env.step("echo")])
    rid = run_id()
    env.launcher.start(plan, context(), rid)
    state, running = env.launcher.wait(rid, 60)
    assert not running and state.status == "complete", state
    assert state.step_status == ("succeeded", "succeeded") and len(state.nonce) == 16
    rundir = env.state / "debug-runs" / rid
    text = env.output(rid + "-s0")
    assert "SONDER_%s_BEGIN" % state.nonce in text and "SONDER_%s_END" % state.nonce in text
    seen = json.loads(text.splitlines()[1])
    assert seen["cwd"] == str(rundir / "cwd") and seen["rundir"] == str(rundir)
    assert seen["env"].get("HOME") == str(rundir / "home")
    assert "SONDER_LAUNCHER_LEAK_CHECK" not in seen["env"]
    assert "DEBUGGER_LEAK_CANARY" not in seen["env"] and "GDBHISTFILE" not in seen["env"]
    assert seen["input"].startswith(str(rundir / "in"))
    assert bytes.fromhex(seen["data"]) == (env.allowed / "core.1").read_bytes()[:16]
    # the plan kept its placeholders; only the launched argv carries values
    assert plan.steps[0].template_argv[3] == "{nonce}"
    view = env.registry.view(rid + "-s1")
    assert view.metadata["run_id"] == rid and view.metadata["step"] == "1"
    assert view.metadata["principal_id"] == "owner" and view.metadata["command_digest"] == "c" * 64
    assert view.metadata["input_sha256"] == ident.sha256
    assert view.record.identity.kind == "tool.crash_digest"
    # staged capture, HOME, TMP and cwd are gone; only the small records stay
    assert sorted(os.listdir(rundir)) == ["chain.json", "plan.json"]
    if os.name != "nt":  # Windows st_mode carries only the read-only bit; the launcher skips chmod there
        assert stat.S_IMODE(os.stat(rundir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(env.state / "debug-runs").st_mode) == 0o700
        assert stat.S_IMODE(os.stat(rundir / "plan.json").st_mode) == 0o600


def test_every_run_gets_a_fresh_nonce(env):
    ident = env.capture()
    nonces = set()
    for _ in range(2):
        rid = run_id()
        env.launcher.start(env.plan(ident, [env.step("echo")]), context(), rid)
        state, _ = env.launcher.wait(rid, 60)
        nonces.add(state.nonce)
    assert len(nonces) == 2


def test_the_deadline_times_out_and_kills_the_tree(env, tmp_path):
    ident = env.capture()
    pid_file = tmp_path / "pids.txt"
    rid = run_id()
    env.launcher.start(env.plan(ident, [env.step("sleep", str(pid_file), timeout=2),
                                        env.step("echo")]), context(), rid)
    pids = _pids(pid_file)
    state, running = env.launcher.wait(rid, 60)
    assert not running and state.status == "timed_out", state
    assert state.step_status == ("timed_out",)  # the chain stopped
    deadline = time.monotonic() + 15
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(_alive(pid) for pid in pids), "the process tree outlived the deadline"


def test_cancel_mid_chain_stops_the_step_and_the_rest(env, tmp_path):
    ident = env.capture()
    pid_file = tmp_path / "pids.txt"
    rid = run_id()
    env.launcher.start(env.plan(ident, [env.step("sleep", str(pid_file)), env.step("echo")]),
                       context(), rid)
    pids = _pids(pid_file)
    assert env.launcher.cancel(rid, "operator cancel") is True
    state, running = env.launcher.wait(rid, 30)
    assert not running and state.status == "cancelled"
    assert env.registry.get(rid + "-s1") is None
    deadline = time.monotonic() + 15
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(_alive(pid) for pid in pids)


def test_a_flooding_step_is_stopped_at_the_output_limit_with_a_bounded_registry(env):
    launcher = env.make_launcher(output_limit_bytes=4 << 20, watchdog_seconds=0.2)
    ident = env.capture()
    rid = run_id()
    started = time.monotonic()
    launcher.start(env.plan(ident, [env.step("flood"), env.step("echo")]), context(), rid)
    state, running = launcher.wait(rid, 90)
    assert not running and state.output_limit and state.status == "partial", state
    assert time.monotonic() - started < 60
    retained = 0
    after = None
    while True:
        page = env.registry.stream(rid + "-s0", after=after, max_events=256, max_bytes=1 << 20)
        retained += sum(len(event.data) for event in page.events)
        if not page.has_more or not page.events:
            break
        after = page.next_watermark
    assert retained <= 64 * 1024 + 8192  # the registry keeps a bounded tail
    assert env.registry.get(rid + "-s1") is None


def test_a_changed_input_is_detected_after_the_run(env):
    ident = env.capture()
    rid = run_id()
    env.launcher.start(env.plan(ident, [env.step("touch")], staging="path"), context(), rid)
    state, _ = env.launcher.wait(rid, 60)
    assert state.input_changed is True


def test_a_copied_input_is_immune_to_the_debugger_writing_it(env):
    ident = env.capture()
    rid = run_id()
    env.launcher.start(env.plan(ident, [env.step("touch")]), context(), rid)
    state, _ = env.launcher.wait(rid, 60)
    assert state.input_changed is False and state.status == "complete"


def test_the_executable_guard_runs_again_at_launch(env):
    ident = env.capture()
    env.refuse.add(sys.executable)
    with pytest.raises(PermissionError):
        env.launcher.start(env.plan(ident, [env.step("echo")]), context(), run_id())
    assert not (env.state / "debug-runs").exists() or os.listdir(env.state / "debug-runs") == []


def test_a_step_refused_between_start_and_launch_fails_the_chain(env):
    ident = env.capture()
    plan = env.plan(ident, [env.step("echo")])
    plan = replace(plan, checked_executables=())
    env.refuse.add(sys.executable)
    rid = run_id()
    env.launcher.start(plan, context(), rid)
    state, _ = env.launcher.wait(rid, 30)
    assert state.status == "failed" and state.step_status == ("start_failed",)


def test_a_failing_step_is_reported_and_the_chain_continues(env):
    ident = env.capture()
    rid = run_id()
    env.launcher.start(env.plan(ident, [env.step("fail"), env.step("echo")]), context(), rid)
    state, _ = env.launcher.wait(rid, 60)
    assert state.step_status == ("failed", "succeeded") and state.status == "failed"
    assert state.step_exit_codes[0] == 3


def test_file_outputs_are_kept_for_assembly_until_the_result_is_stored(env):
    ident = env.capture()
    rid = run_id()
    env.launcher.start(env.plan(ident, [env.step("file", via="file:{rundir}\\out\\cpu.txt")]),
                       context(), rid)
    state, _ = env.launcher.wait(rid, 60)
    assert state.status == "complete"
    assert env.launcher.step_output(rid, 0) == "Function,Weight\nmain,10\n"
    fresh = env.make_launcher()
    assert fresh.step_output(rid, 0) == "Function,Weight\nmain,10\n"
    fresh.store_json(rid, "context.json", {"kind": "crash"})
    fresh.store_json(rid, "result.json", {"status": "complete"})
    rundir = env.state / "debug-runs" / rid
    assert sorted(os.listdir(rundir)) == ["chain.json", "plan.json", "result.json"]
    assert fresh.load_json(rid, "result.json") == {"status": "complete"}
    with pytest.raises(ValueError):
        fresh.store_json(rid, "../escape.json", {})
    with pytest.raises(KeyError):
        fresh.load_json("../../etc", "plan.json")


def test_ownership_and_state_survive_a_new_launcher(env):
    ident = env.capture()
    rid = run_id()
    env.launcher.start(env.plan(ident, [env.step("echo")]), context("alice"), rid)
    env.launcher.wait(rid, 60)
    fresh = env.make_launcher()
    assert fresh.metadata(rid)["principal_id"] == "alice"
    assert fresh.metadata(rid)["kind"] == "tool.crash_digest"
    state, running = fresh.wait(rid, 0)
    assert not running and state.status == "complete" and state.nonce
    assert fresh.metadata("debug-run-" + "0" * 32) is None
    assert fresh.running_for("alice") == 0


def test_a_run_interrupted_by_a_restart_reads_as_failed(env):
    rid = run_id()
    rundir = env.state / "debug-runs" / rid
    rundir.mkdir(parents=True)
    (rundir / "plan.json").write_text(json.dumps({"principal_id": "owner", "kind": "tool.crash_digest",
                                                  "step_job_ids": [rid + "-s0"]}))
    state, running = env.make_launcher().wait(rid, 0)
    assert not running and state.status == "failed"


def test_run_dirs_are_pruned_to_the_newest_32_and_orphans_are_scrubbed(env):
    root = env.state / "debug-runs"
    root.mkdir(parents=True)
    for index in range(40):
        old = root / ("debug-run-%032x" % index)
        (old / "in").mkdir(parents=True)
        (old / "in" / "capture.dmp").write_bytes(b"memory")
        (old / "plan.json").write_text("{}")
        os.utime(old, (1000 + index, 1000 + index))
    ident = env.capture()
    rid = run_id()
    env.launcher.start(env.plan(ident, [env.step("echo")]), context(), rid)
    env.launcher.wait(rid, 60)
    names = os.listdir(root)
    assert len(names) <= 32 and rid in names
    for name in names:
        assert not (root / name / "in").exists()


def test_running_for_counts_only_live_runs(env, tmp_path):
    ident = env.capture()
    rid = run_id()
    env.launcher.start(env.plan(ident, [env.step("sleep", str(tmp_path / "p.txt"))]), context(), rid)
    _pids(tmp_path / "p.txt")
    assert env.launcher.running_for("owner") == 1 and env.launcher.running_for("other") == 0
    env.launcher.cancel(rid, "done")
    assert env.launcher.running_for("owner") == 0


def test_the_service_caps_concurrent_runs_per_caller(env, tmp_path):
    from sonder_runtime.application.debugging.ports import CrashDigestRequest
    from sonder_runtime.application.debugging.service import DebugDigestService
    from sonder_runtime.domain.common.errors import SonderError
    from tests.test_debug_service import FakeConsent, FakeOutput, FakeTriage

    ident = env.capture()

    class Planner:
        def plan_crash(self, request, ctx, *, network_allowed, identity, tier0):
            name = "p%d.txt" % len(os.listdir(tmp_path))
            return env.plan(identity, [env.step("sleep", str(tmp_path / name))])

    class Source:
        def open_reader(self, path, **kwargs):
            return env.source.open_reader(path)

        def is_dir(self, path, **kwargs):
            return False

    service = DebugDigestService(Source(), FakeTriage(), Planner(), env.launcher, FakeOutput(),
                                 consent=FakeConsent(), source_map=None, redact=lambda t: t,
                                 clock=time.time)
    request = CrashDigestRequest(str(env.allowed / "core.1"))
    first = service.crash(request, context(), wait_seconds=0)
    second = service.crash(request, context(), wait_seconds=0)
    assert first.status == second.status == "running"
    with pytest.raises(SonderError) as caught:
        service.crash(request, context(), wait_seconds=0)
    assert caught.value.code == "DEBUG_RUN_BUSY"
    other = service.crash(request, context("someone-else"), wait_seconds=0)
    for outcome in (first, second, other):
        env.launcher.cancel(outcome.run_id, "test over")
    assert ident.kind == "elf_core"
