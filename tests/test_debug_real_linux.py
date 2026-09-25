"""Real processes on a Linux host: g++ crashers, gdb/lldb/eu-stack on real ELF
cores, llvm-symbolizer on a clang-cl PE+PDB, sanitizer logs, bucketing, the
deadline and no debuginfod egress. Each case skips when its tool is absent.

Fixtures are generated at test time in a temporary directory (a core holds
process memory; none is committed).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

pytest.importorskip("sonder_runtime.domain.debugging.templates")
pytest.importorskip("sonder_runtime.domain.crash.model")
pytest.importorskip("sonder_runtime.domain.profiling.model")

from sonder_runtime.adapters.debugging.capture_source import GuardedCaptureSource  # noqa: E402
from sonder_runtime.adapters.debugging.launcher import ProcessDebugLauncher  # noqa: E402
from sonder_runtime.adapters.debugging.planner import HostDebugPlanner  # noqa: E402
from sonder_runtime.adapters.debugging.source_map import ProjectSourceMap  # noqa: E402
from sonder_runtime.adapters.debugging.triage import PureCaptureTriage  # noqa: E402
from sonder_runtime.adapters.diagnostics.sources import RegistryJobOutputReader  # noqa: E402
from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider  # noqa: E402
from sonder_runtime.adapters.host_tools.bounded_process import run_bounded  # noqa: E402
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry  # noqa: E402
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor  # noqa: E402
from sonder_runtime.application.context import local_owner_context  # noqa: E402
from sonder_runtime.application.debugging.ports import (  # noqa: E402
    CrashDigestRequest,
    CrashTriageRequest,
)
from sonder_runtime.application.debugging.service import DebugDigestService  # noqa: E402
from sonder_runtime.platform import netns_probe  # noqa: E402
from sonder_runtime.platform.symbol_consent import SymbolConsentState  # noqa: E402

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux host only")]

CRASHER = r"""
#include <cstdio>
#include <cstdlib>
#include <cstring>
struct Renderer { virtual void Submit(int *p); virtual ~Renderer() {} };
__attribute__((noinline)) void Renderer::Submit(int *p) {
    *p = 42;
}
__attribute__((noinline)) void Frame(Renderer &r, int *p) { r.Submit(p); }
__attribute__((noinline)) void Fail() { std::abort(); }
int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "null";
    if (!std::strcmp(mode, "abort")) { Fail(); }
    if (!std::strcmp(mode, "uaf")) {
        int *p = new int[4];
        delete[] p;
        std::printf("%d\n", p[1]);
        return 0;
    }
    Renderer r;
    int *p = argc > 5 ? new int : nullptr;
    Frame(r, p);
    return 0;
}
"""


@dataclass(frozen=True)
class Record:
    name: str
    path: str
    version: str = ""
    identity: str = ""


class HostLookup:
    def __init__(self, overrides=None):
        self.overrides = dict(overrides or {})

    def lookup(self, name):
        if name in self.overrides:
            return Record(name, self.overrides[name])
        path = shutil.which(name)
        return Record(name, os.path.abspath(path)) if path else None


class Stack:
    def __init__(self, tmp_path: Path, *, lookup=None, isolation=True) -> None:
        self.allowed = tmp_path / "allowed"
        self.allowed.mkdir(parents=True, exist_ok=True)
        self.state = tmp_path / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
        self.provider = SubprocessJobProvider(self.registry, process_cleanup=ProcessTreeSupervisor())
        self.source = GuardedCaptureSource()
        self.planner = HostDebugPlanner(
            lookup or HostLookup(), redact=lambda t: t, system="Linux",
            isolation_probe=(lambda path: netns_probe.netns_available(path, runner=run_bounded))
            if isolation else None,
            source=self.source, state_dir=str(self.state))
        self.launcher = ProcessDebugLauncher(lambda: self.provider, lambda: self.registry,
                                             executable_guard=lambda path: path,
                                             run_root=str(self.state / "debug-runs"), source=self.source)
        self.service = DebugDigestService(
            self.source, PureCaptureTriage(), self.planner, self.launcher,
            RegistryJobOutputReader(lambda: self.registry), consent=SymbolConsentState(),
            source_map=ProjectSourceMap([self.allowed]), redact=lambda t: t, clock=time.time)


def ctx():
    return local_owner_context(correlation_id=uuid.uuid4().hex)


def _need(*tools):
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.skip("not installed: %s" % ", ".join(missing))


def _core(workdir: Path, binary: Path, mode: str, name: str) -> Path:
    core = workdir / name
    subprocess.run(["gdb", "-nx", "-batch", "-ex", "run", "-ex", "generate-core-file %s" % core,
                    "--args", str(binary), mode], capture_output=True, timeout=120, cwd=workdir)
    if not core.exists():
        pytest.skip("gdb could not produce a core on this host")
    return core


@pytest.fixture(scope="module")
def crasher(tmp_path_factory):
    _need("g++", "gdb")
    work = tmp_path_factory.mktemp("crasher")
    source = work / "crasher.cpp"
    source.write_text(CRASHER)
    binary = work / "crasher"
    subprocess.run(["g++", "-g", "-O0", "-o", str(binary), str(source)], check=True,
                   capture_output=True, timeout=180)
    core = _core(work, binary, "null", "core.null")
    return {"work": work, "binary": binary, "source": source, "core": core}


def _into(stack: Stack, crasher, *names):
    for name in names:
        shutil.copy(crasher[name], stack.allowed / crasher[name].name)
    return [stack.allowed / crasher[name].name for name in names]


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path / "allowed"))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
    return Stack(tmp_path)


def _top(report):
    thread = report.crashing_thread()
    return thread.frames[0]


@pytest.mark.parametrize("engine,tool", [("gdb", "gdb"), ("lldb", "lldb"), ("eu_stack", "eu-stack")])
def test_a_gxx_core_through_crash_digest(stack, crasher, engine, tool):
    _need(tool)
    core, binary, _source = _into(stack, crasher, "core", "binary", "source")
    triage = stack.service.triage(CrashTriageRequest(str(core)), ctx())
    assert triage.exception.name == "SIGSEGV" and triage.engines == ("pure",)
    outcome = stack.service.crash(CrashDigestRequest(str(core), executable=str(binary), engine=engine),
                                  ctx(), wait_seconds=90)
    assert outcome.status == "complete", outcome
    report = outcome.crash
    assert report.engines == ("pure", engine)
    top = _top(report)
    assert "Renderer::Submit" in top.function and top.file.endswith("crasher.cpp") and top.line == 7
    assert top.local_file == "crasher.cpp"
    assert any(hint.kind == "null_deref" for hint in report.hints)
    assert report.symbolication == "full" and report.signature_basis == "functions"
    expected_isolation = "netns" if netns_probe.netns_available(shutil.which("unshare") or "",
                                                                 runner=run_bounded) else "none"
    assert report.egress_isolation == expected_isolation
    rundir = stack.state / "debug-runs" / outcome.run_id
    assert sorted(os.listdir(rundir)) == ["chain.json", "plan.json", "result.json"]


def test_the_three_debuggers_agree_on_the_signature(stack, crasher):
    _need("lldb", "eu-stack")
    core, binary = _into(stack, crasher, "core", "binary")
    signatures = {stack.service.crash(CrashDigestRequest(str(core), executable=str(binary), engine=engine),
                                      ctx(), wait_seconds=90).crash.signature
                  for engine in ("gdb", "lldb", "eu_stack")}
    assert len(signatures) == 1


def test_debuginfod_urls_in_the_parent_environment_cause_no_connection(tmp_path, crasher, monkeypatch):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    hits = []
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            hits.append(1)
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    monkeypatch.setenv("DEBUGINFOD_URLS", "http://127.0.0.1:%d" % port)
    try:
        for isolation in (True, False):  # the environment alone already prevents it
            workdir = tmp_path / ("iso" if isolation else "plain")
            monkeypatch.setenv("SONDER_FILE_ROOTS", str(workdir / "allowed"))
            stack = Stack(tmp_path / ("iso" if isolation else "plain"), isolation=isolation)
            core, binary = _into(stack, crasher, "core", "binary")
            started = time.monotonic()
            outcome = stack.service.crash(CrashDigestRequest(str(core), executable=str(binary)), ctx(),
                                          wait_seconds=90)
            assert outcome.status == "complete" and outcome.network is False
            assert time.monotonic() - started < 60
    finally:
        stop.set()
        thread.join(2)
        listener.close()
    assert hits == []


def test_an_asan_log_through_crash_triage(stack, crasher):
    _need("g++")
    binary = crasher["work"] / "crasher_asan"
    try:
        subprocess.run(["g++", "-g", "-O0", "-fsanitize=address", "-o", str(binary), str(crasher["source"])],
                       check=True, capture_output=True, timeout=180)
    except subprocess.CalledProcessError:
        pytest.skip("AddressSanitizer is not available to g++ here")
    run = subprocess.run([str(binary), "uaf"], capture_output=True, text=True, timeout=60,
                         env={"PATH": "/usr/bin:/bin", "ASAN_OPTIONS": "detect_leaks=0"})
    log = stack.allowed / "asan_uaf.log"
    log.write_text(run.stderr)
    report = stack.service.triage(CrashTriageRequest(str(log)), ctx())
    assert report.source_kind == "sanitizer_report"
    assert "heap-use-after-free" in report.exception.name
    assert report.crashing_thread().frames[0].line is not None


def test_a_directory_of_three_cores_buckets_into_two_signatures(stack, crasher):
    folder = stack.allowed / "dumps"
    folder.mkdir()
    shutil.copy(crasher["core"], folder / "core.a")
    shutil.copy(crasher["core"], folder / "core.b")
    abort_core = _core(crasher["work"], crasher["binary"], "abort", "core.abort")
    shutil.copy(abort_core, folder / "core.c")
    buckets = stack.service.triage(CrashTriageRequest(str(folder)), ctx())
    assert len(buckets) == 2
    assert sorted(bucket.count for bucket in buckets) == [1, 2]


def test_a_deadline_times_out_a_hung_debugger_with_no_survivors(tmp_path, crasher, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path / "allowed"))
    pid_file = tmp_path / "pids"
    fake = tmp_path / "bin" / "gdb"
    fake.parent.mkdir()
    fake.write_text("#!%s\nimport os, subprocess, sys, time\n"
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
                    "open(%r, 'w').write('%%d %%d' %% (os.getpid(), child.pid))\n"
                    "time.sleep(300)\n" % (sys.executable, str(pid_file)))
    fake.chmod(0o755)
    stack = Stack(tmp_path, lookup=HostLookup({"gdb": str(fake)}), isolation=False)
    real_plan = stack.planner.plan_crash

    def one_second(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        return replace(plan, steps=tuple(replace(step, timeout_seconds=1) for step in plan.steps))

    stack.planner.plan_crash = one_second
    core, binary = _into(stack, crasher, "core", "binary")
    started = time.monotonic()
    outcome = stack.service.crash(CrashDigestRequest(str(core), executable=str(binary), engine="gdb"),
                                  ctx(), wait_seconds=60)
    assert outcome.status == "timed_out", outcome
    assert time.monotonic() - started < 45
    assert outcome.crash is not None and outcome.crash.engines == ("pure",)
    pids = [int(item) for item in pid_file.read_text().split()]
    deadline = time.monotonic() + 15

    def alive(pid):
        try:
            state = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, IndexError):
            return False
        return state not in {"Z", "X"}

    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(alive(pid) for pid in pids)


# -- (d) llvm-symbolizer on a clang-cl PE+PDB with a synthetic minidump ------------------------

TINY_C = r"""
__declspec(noinline) int helper(int value) {
    return value * 3 + 1;
}

int main(void) {
    volatile int *pointer = 0;
    return helper(*pointer);
}
"""


def _build_pe(out: Path, pdb_alt: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    source = out / "spark_tiny.c"
    source.write_text(TINY_C)
    subprocess.run(["clang-cl-18", "--target=x86_64-pc-windows-msvc", "/Z7", "/O1", "/GS-", "/c",
                    "/Fo" + str(out / "spark_tiny.obj"), "--", str(source)],
                   check=True, capture_output=True, cwd=out, timeout=120)
    subprocess.run(["lld-link-18", "/debug", "/entry:main", "/subsystem:console", "/nodefaultlib",
                    str(out / "spark_tiny.obj"), "/out:" + str(out / "spark_tiny.exe"),
                    "/pdb:" + str(out / "spark_tiny.pdb"), "/pdbaltpath:" + pdb_alt],
                   check=True, capture_output=True, cwd=out, timeout=120)
    return out / "spark_tiny.exe"


def _dump(image: Path) -> bytes:
    builder = pytest.importorskip("tests.support.minidump_builder")
    from sonder_runtime.domain.binaries.pe_debug import read_pe_identity
    from sonder_runtime.domain.binaries.reader import BytesReader

    pe = read_pe_identity(BytesReader(image.read_bytes()))
    base, sp = 0x7FF6_1000_0000, 0xC1_2F8F_0100
    dump = builder.MinidumpBuilder().system_info(builder.AMD64, builder.WIN32NT)
    dump.module("C:\\build\\out\\spark_tiny.exe", base, 0x4000,
                cv=builder.rsds_record(pe.rsds_guid, pe.rsds_age, pe.pdb_path))
    dump.thread(0x10, pc=base + 0x1003, sp=sp, stack=builder.stack_with_returns(sp, [base + 0x1016]))
    dump.exception(0x10, 0xC0000005, address=base + 0x1003, params=(0, 0), pc=base + 0x1003, sp=sp)
    return dump.build()


def test_llvm_symbolizer_gives_file_and_line_for_a_verified_pe(stack):
    _need("clang-cl-18", "lld-link-18", "llvm-symbolizer")
    syms = stack.allowed / "syms"
    image = _build_pe(syms, "C:\\build\\out\\spark_tiny.pdb")
    for extra in ("spark_tiny.obj", "spark_tiny.c"):
        (syms / extra).unlink()
    dump = stack.allowed / "tiny.dmp"
    dump.write_bytes(_dump(image))
    outcome = stack.service.crash(CrashDigestRequest(str(dump), symbol_dirs=(str(syms),),
                                                     engine="llvm_symbolizer"), ctx(), wait_seconds=90)
    assert outcome.status == "complete", outcome
    top = _top(outcome.crash)
    assert top.function == "helper" and top.file.endswith("spark_tiny.c") and top.line
    assert outcome.crash.module_named("spark_tiny.exe").symbols == "loaded"


def test_an_rsds_path_naming_a_fifo_plans_no_step_and_finishes_fast(stack, tmp_path):
    _need("clang-cl-18", "lld-link-18", "llvm-symbolizer")
    fifo = tmp_path / "trap.pdb"
    os.mkfifo(fifo)
    build = tmp_path / "build"
    image = _build_pe(build, str(fifo))
    syms = stack.allowed / "syms"
    syms.mkdir()
    shutil.copy(image, syms / "spark_tiny.exe")  # no PDB next to the exe
    dump = stack.allowed / "fifo.dmp"
    dump.write_bytes(_dump(image))
    started = time.monotonic()
    outcome = stack.service.crash(CrashDigestRequest(str(dump), symbol_dirs=(str(syms),),
                                                     engine="llvm_symbolizer"), ctx(), wait_seconds=30)
    assert time.monotonic() - started < 10
    assert outcome.run_id == "" and outcome.status == "complete"
    assert "llvm_symbolizer" not in outcome.crash.engines


def test_minidump_stackwalk_on_a_real_breakpad_dump():
    _need("minidump-stackwalk")
    pytest.skip("no Linux Breakpad minidump writer is installed on this host")


# -- nothing executes from the capture or the executable's directory ------------------------


def test_gdb_does_not_auto_load_scripts_beside_the_core_or_the_executable(stack, crasher, tmp_path):
    _need("gdb")
    core, binary = _into(stack, crasher, "core", "binary")
    marker = tmp_path / "AUTO_LOADED"
    for folder in {core.parent, binary.parent}:
        (folder / ".gdbinit").write_text("shell touch %s\n" % marker)
    (binary.parent / (binary.name + "-gdb.py")).write_text("open(%r, 'w').write('x')\n" % str(marker))
    (binary.parent / (binary.name + "-gdb.gdb")).write_text("shell touch %s\n" % marker)
    outcome = stack.service.crash(CrashDigestRequest(str(core), executable=str(binary), engine="gdb"),
                                  ctx(), wait_seconds=90)
    assert outcome.status == "complete", outcome
    assert not marker.exists()


@pytest.mark.parametrize("engine", ["gdb", "lldb"])
def test_hostile_capture_and_executable_names_are_plain_arguments(stack, crasher, engine):
    _need(engine)
    folder = stack.allowed / "d;$(touch PWNED)'\"`touch PWNED`"
    folder.mkdir()
    core = folder / "core;$(touch PWNED).-ex shell touch PWNED"
    binary = folder / "-ex shell touch PWNED"
    shutil.copy(crasher["core"], core)
    shutil.copy(crasher["binary"], binary)
    outcome = stack.service.crash(CrashDigestRequest(str(core), executable=str(binary), engine=engine),
                                  ctx(), wait_seconds=90)
    assert outcome.status == "complete", outcome
    rundir = stack.state / "debug-runs" / outcome.run_id
    assert not any(path.name == "PWNED" for path in stack.state.rglob("*"))
    assert not any(path.name == "PWNED" for path in stack.allowed.rglob("*"))
    assert sorted(os.listdir(rundir)) == ["chain.json", "plan.json", "result.json"]


def test_a_launched_debugger_sees_only_the_host_owned_argv_environment_and_cwd(tmp_path, crasher,
                                                                              monkeypatch):
    """A recording shim stands in for gdb under the real provider."""
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path / "allowed"))
    for key in ("LD_PRELOAD", "PYTHONPATH", "GDBHISTFILE", "LLDB_DEBUGSERVER_PATH", "_NT_SYMBOL_PATH",
                "DEBUGINFOD_CACHE_PATH", "INIT", "DEBUGGER_LEAK_CANARY"):
        monkeypatch.setenv(key, "/nonexistent/should-not-leak")
    monkeypatch.setenv("DEBUGINFOD_URLS", "http://127.0.0.1:9")
    record = tmp_path / "seen.json"
    fake = tmp_path / "bin" / "gdb"
    fake.parent.mkdir()
    fake.write_text("#!%s -I\nimport json, os, sys\njson.dump({'argv': sys.argv, 'env': dict(os.environ), "
                    "'cwd': os.getcwd(), 'cwd_entries': os.listdir('.')}, open(%r, 'w'))\n"
                    % (sys.executable, str(record)))
    fake.chmod(0o755)
    stack = Stack(tmp_path, lookup=HostLookup({"gdb": str(fake)}), isolation=False)
    core, binary = _into(stack, crasher, "core", "binary")
    stack.service.crash(CrashDigestRequest(str(core), executable=str(binary), engine="gdb"), ctx(),
                        wait_seconds=60)
    import json

    seen = json.loads(record.read_text())
    assert set(seen["env"]) <= {"PATH", "HOME", "LANG", "TMPDIR", "DEBUGINFOD_URLS", "PERF_CONFIG",
                                "LC_CTYPE"}, seen["env"]
    assert seen["env"]["DEBUGINFOD_URLS"] == "" and seen["env"]["PATH"] == "/usr/bin:/bin"
    assert seen["cwd"].endswith("/cwd") and seen["cwd_entries"] == []
    assert seen["env"]["HOME"].startswith(str(stack.state / "debug-runs"))
    argv = seen["argv"][1:]
    assert argv[:4] == ["-nx", "-nh", "-batch", "-q"]
    assert argv[argv.index("-iex") + 1] == "set auto-load off"
    assert argv[-2] == str(binary) and argv[-1].startswith(str(stack.state / "debug-runs"))
    assert not any(item in ("-x", "--command", "source") for item in argv)
