"""Real build jobs: the durable launcher, the private tee log, cancellation,
deadlines, the scrubbed environment, and (with the build domain) the whole
plan -> run -> collect path on a tiny CMake project with Ninja and Make.

The durable pieces are the production ones (SQLite job registry, subprocess
job provider with process-tree supervision). The inventory lookup and the
host-executable guard are minimal doubles faithful to their ports, as in the
structured-test harness.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from sonder_runtime.adapters.build.environment import ScrubbedEnvironmentProvider
from sonder_runtime.adapters.build.launcher import (
    LOG_FILE_NAME,
    ProcessBuildLauncher,
    QUERY_RELATIVE,
    write_file_api_query,
)
from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.build.ports import BUILD_JOB_PREFIX, BuildJobPlan
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.jobs import JobStatus
from sonder_runtime.domain.common.errors import SonderError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics; Windows uses job objects"),
]

FAKE_SECRET = "pretend-credential-for-env-dump"


@dataclass(frozen=True)
class HostRecord:
    name: str
    path: str
    version: str = ""
    details: tuple = ()


class HostLookup:
    """``HostToolLookup`` double: ``shutil.which`` (the PATH entry, not its realpath)."""

    def lookup(self, name):
        path = shutil.which(name)
        return None if path is None else HostRecord(name, os.path.abspath(path))


def host_executable_guard(path: str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.resolve().is_file():
        raise PermissionError("host executable rejected")
    return path


class LauncherStack:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
        self.provider = SubprocessJobProvider(self.registry, process_cleanup=ProcessTreeSupervisor())
        self.run_root = self.state / "build-runs"
        self.launcher = ProcessBuildLauncher(lambda: self.provider, lambda: self.registry,
                                             executable_guard=host_executable_guard,
                                             run_root=str(self.run_root))
        self.env = ScrubbedEnvironmentProvider(host="posix", project_local=lambda path: False)

    @staticmethod
    def context():
        return local_owner_context(correlation_id=uuid.uuid4().hex)

    def plan(self, argv, *, cwd=None, timeout=60, action="build", build_dir=None, pre_writes=()):
        token = uuid.uuid4().hex
        log_dir = self.run_root / (BUILD_JOB_PREFIX + token)
        env = self.env.environment(system="cmake", family="gnu")
        cwd = str(cwd or self.tmp)
        return BuildJobPlan(
            action=action, system="cmake", project_root=str(self.tmp), build_dir=str(build_dir or self.tmp / "b"),
            cwd=cwd, argv=tuple(argv), display_argv=tuple(Path(argv[0]).name for _ in (0,)) + tuple(argv[1:]),
            cwd_label="proj", command_digest="d" * 64, environment=env.pairs, env_keys=env.keys,
            timeout_seconds=timeout, max_descendants=64, memory_limit_bytes=None,
            log_dir=str(log_dir), log_file=str(log_dir / LOG_FILE_NAME), binlog="", world="host",
            network="advisory_off", isolation_truth="unverified", model_digest="m",
            template_id="test", checked_executables=(argv[0],), run_token=token,
            pre_writes=tuple(pre_writes),
        )

    def start(self, plan, **kwargs):
        job_id = BUILD_JOB_PREFIX + plan.run_token
        self.launcher.start(plan, self.context(), job_id, **kwargs)
        return job_id

    def finish(self, job_id, limit=60.0):
        record, exit_code, timed_out = self.launcher.wait(job_id, limit)
        assert not timed_out, "job did not finish"
        return record, exit_code


@pytest.fixture
def lstack(tmp_path):
    return LauncherStack(tmp_path)


def _sh(script):
    return ["/bin/sh", "-c", script]


def test_output_is_teed_to_a_private_log_and_the_exit_code_kept(lstack):
    exits = []
    plan = lstack.plan(_sh("echo one; echo two >&2; printf 'caf\\303\\251 \\377\\n'; exit 3"))
    job = lstack.start(plan, on_exit=exits.append)
    record, exit_code = lstack.finish(job)
    assert record.status is JobStatus.FAILED and exit_code == 3
    log = Path(plan.log_file)
    text = log.read_bytes()
    assert b"one\n" in text and b"two\n" in text and b"caf\xc3\xa9" in text
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(plan.log_dir).stat().st_mode) == 0o700
    meta = lstack.launcher.metadata(job)
    assert meta["kind"] == "tool.build_job" and meta["log_file"] == plan.log_file
    deadline = time.monotonic() + 5
    while not exits and time.monotonic() < deadline:
        time.sleep(0.05)
    assert exits == [job]


def test_the_registry_gets_only_a_bounded_prefix(lstack):
    lstack.launcher._forward_bytes = 1000
    plan = lstack.plan(_sh("i=0; while [ $i -lt 3000 ]; do echo line-$i-xxxxxxxxxxxxxxxx; i=$((i+1)); done"))
    job = lstack.start(plan)
    lstack.finish(job)
    log = Path(plan.log_file).read_text()
    assert "line-2999-" in log
    page = lstack.registry.stream(job, after=None, max_events=512, max_bytes=1 << 20)
    forwarded = "".join(event.data for event in page.events)
    assert "line-2999-" not in forwarded and "full output is in the private build log" in forwarded


def test_the_environment_is_scrubbed(lstack, monkeypatch):
    monkeypatch.setenv("SONDER_TEST_API_KEY", FAKE_SECRET)
    monkeypatch.setenv("SOME_RANDOM_OPERATOR_VAR", "leak")
    lstack.env = ScrubbedEnvironmentProvider(host="posix", project_local=lambda path: False)
    plan = lstack.plan(["/usr/bin/env"])
    job = lstack.start(plan)
    lstack.finish(job)
    dump = Path(plan.log_file).read_text()
    keys = {line.split("=", 1)[0] for line in dump.splitlines() if "=" in line}
    assert FAKE_SECRET not in dump and "SOME_RANDOM_OPERATOR_VAR" not in keys
    assert "PATH" in keys and "LC_ALL" in keys
    assert keys <= {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "SHELL", "LANG", "LC_ALL", "TERM",
                    "NO_COLOR", "CLICOLOR", "CMAKE_COLOR_DIAGNOSTICS", "GCC_COLORS", "NINJA_STATUS",
                    "PWD", "SHLVL", "_", "LC_CTYPE"}


def _descendants_gone(pids):
    return all(not Path("/proc/%d" % pid).exists() or
               Path("/proc/%d/stat" % pid).read_text().split(")")[-1].split()[0] == "Z"
               for pid in pids)


def _pids_with_marker(marker):
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if marker.encode() in cmdline:
            found.append(int(entry.name))
    return found


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc")
def test_cancel_kills_the_whole_tree(lstack):
    marker = "sonder-cancel-%s" % uuid.uuid4().hex[:8]
    plan = lstack.plan(_sh("sleep 600 & sleep 600; : %s" % marker), timeout=300)
    job = lstack.start(plan)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(_pids_with_marker(marker)) < 1:
        time.sleep(0.05)
    group = os.getpgid(_pids_with_marker(marker)[0])
    members: list[int] = []
    while time.monotonic() < deadline and len(members) < 4:
        members = [pid for pid in (int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit())
                   if _pgid(pid) == group]
        time.sleep(0.05)
    assert len(members) >= 4  # tee, shell, sleeps
    assert lstack.launcher.cancel(job, "operator cancel") is True
    record = lstack.launcher.poll(job)
    assert record.status is JobStatus.CANCELLED
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not _descendants_gone(members):
        time.sleep(0.1)
    assert _descendants_gone(members)


def _pgid(pid):
    try:
        return os.getpgid(pid)
    except OSError:
        return -1


def test_the_deadline_cancels_as_timed_out(lstack):
    plan = lstack.plan(_sh("sleep 600"), timeout=1)
    job = lstack.start(plan)
    record, _ = lstack.finish(job, limit=60)
    assert record.status is JobStatus.CANCELLED and "deadline" in record.error


def test_a_plan_without_checked_executables_or_outside_the_root_is_refused(lstack):
    plan = lstack.plan(_sh("true"))
    with pytest.raises(PermissionError):
        lstack.launcher.start(replace(plan, checked_executables=()), lstack.context(),
                              BUILD_JOB_PREFIX + plan.run_token)
    with pytest.raises(PermissionError):
        lstack.launcher.start(replace(plan, log_dir=str(lstack.tmp / "elsewhere")), lstack.context(),
                              BUILD_JOB_PREFIX + plan.run_token)
    with pytest.raises(PermissionError):  # the log dir must be named after the job
        lstack.launcher.start(plan, lstack.context(), BUILD_JOB_PREFIX + uuid.uuid4().hex)
    with pytest.raises(PermissionError):
        lstack.launcher.start(replace(plan, pre_writes=(("/etc/cron.d/x", b"boom"),)),
                              lstack.context(), BUILD_JOB_PREFIX + plan.run_token)


def test_the_file_api_query_is_written_before_a_configure(lstack):
    build = lstack.tmp / "fresh-build"
    query = build.joinpath(*QUERY_RELATIVE)
    plan = lstack.plan(_sh("cat " + str(query)), action="configure", build_dir=build,
                       pre_writes=((str(query), b'{"requests":[]}'),))
    job = lstack.start(plan)
    lstack.finish(job)
    assert Path(plan.log_file).read_text().strip() == '{"requests":[]}'


def test_a_mistyped_source_tree_is_not_used_as_a_build_dir(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.cpp").write_text("int main(){}")
    with pytest.raises(SonderError) as excinfo:
        write_file_api_query(str(source), b"{}")
    assert excinfo.value.code == "BUILD_TREE_REJECTED"
    assert not (source / ".cmake").exists()
    cmake_tree = tmp_path / "b"
    cmake_tree.mkdir()
    (cmake_tree / "CMakeCache.txt").write_text("")
    assert write_file_api_query(str(cmake_tree), b"{}").endswith("query.json")


def test_old_run_dirs_are_pruned_but_active_ones_kept(lstack):
    lstack.launcher._max_retained = 3
    jobs = []
    for _ in range(5):
        plan = lstack.plan(_sh("true"))
        jobs.append(lstack.start(plan))
        lstack.finish(jobs[-1])
        time.sleep(0.02)
    names = sorted(entry.name for entry in lstack.run_root.iterdir())
    assert len(names) <= 3 and jobs[-1] in names


# ---------------------------------------------------------------------------
# The whole stack: plan -> run -> collect on a tiny CMake project.

HAVE_TOOLS = all(shutil.which(name) for name in ("cmake", "ninja", "g++", "make"))

PROJECT_CMAKE = """cmake_minimum_required(VERSION 3.20)
project(Mini LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
add_library(core STATIC src/core/math.cpp src/core/entity.cpp)
target_include_directories(core PUBLIC src/core)
target_precompile_headers(core PRIVATE src/core/pch.h)
add_executable(gen tools/gen.cpp)
add_custom_command(OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/gen.h
  COMMAND gen ${CMAKE_CURRENT_BINARY_DIR}/gen.h DEPENDS gen)
add_custom_target(generated DEPENDS ${CMAKE_CURRENT_BINARY_DIR}/gen.h)
add_executable(game src/game/main.cpp)
add_dependencies(game generated)
target_include_directories(game PRIVATE ${CMAKE_CURRENT_BINARY_DIR})
target_link_libraries(game PRIVATE core)
add_custom_target(deploy COMMAND ${CMAKE_COMMAND} -E echo deploy)
add_custom_target(slow COMMAND sleep 600)
add_custom_target(envdump COMMAND env)
"""
FILES = {
    "src/core/pch.h": "#pragma once\n#include <vector>\n",
    "src/core/math.h": "#pragma once\nstruct Vec3 { float x, y, z; };\nfloat length(const Vec3& v);\n",
    "src/core/math.cpp": ('#include "math.h"\n#include <cmath>\n'
                          "float length(const Vec3& v) { return std::sqrt(v.x * v.x + v.y * v.y + v.z * v.z); }\n"
                          "float twice(const Vec3& v) { return 2.0f * lenght(v); }\n"),
    "src/core/entity.h": '#pragma once\n#include "math.h"\nstruct Entity { Vec3 position{}; float speed() const; };\n',
    "src/core/entity.cpp": '#include "entity.h"\nfloat Entity::speed() const { return length(position); }\n',
    "src/game/main.cpp": ('#include "entity.h"\n#include "gen.h"\n#include <cstdio>\n'
                          'int main() { Entity e; std::printf("%s %f\\n", kGen, e.speed()); return 0; }\n'),
    "tools/gen.cpp": ('#include <fstream>\nint main(int, char** argv) { std::ofstream(argv[1]) << '
                      '"#pragma once\\nstatic const char* kGen = \\"gen\\";\\n"; return 0; }\n'),
}


def write_project(root: Path, prefix: str = "") -> Path:
    root.mkdir(parents=True)
    (root / "CMakeLists.txt").write_text(prefix + PROJECT_CMAKE)
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


class FullStack(LauncherStack):
    def __init__(self, tmp_path: Path, *, network_mode="advisory"):
        super().__init__(tmp_path)
        from sonder_runtime.adapters.build.collector import BuildOutputCollector
        from sonder_runtime.adapters.build.network import NetworkIsolation
        from sonder_runtime.adapters.build.planner import ProjectBuildPlanner
        from sonder_runtime.adapters.build.tree_reader import GuardedBuildTreeReader
        from sonder_runtime.application.build.model_service import BuildModelService, LruBuildModelCache
        from sonder_runtime.application.build.run_service import (
            BuildJobService, InMemoryBuildDirLeases, build_job_liveness,
        )

        self.reader = GuardedBuildTreeReader()
        self.network = NetworkIsolation(mode=network_mode, lookup=HostLookup(),
                                        executable_guard=host_executable_guard)
        self.planner = ProjectBuildPlanner(HostLookup(), self.reader, self.env, self.network,
                                           run_root=str(self.run_root), host="linux",
                                           executable_guard=host_executable_guard)
        self.models = BuildModelService(self.reader, self.planner, LruBuildModelCache(), clock=time.time)
        self.collector = BuildOutputCollector(str(self.run_root))
        self.jobs = BuildJobService(self.planner, self.launcher, self.collector, self.models,
                                    InMemoryBuildDirLeases(is_active=build_job_liveness(self.launcher)),
                                    clock=time.time)

    def result(self, job_id, limit=240.0):
        from sonder_runtime.domain.build.report import BuildJobReport

        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            result = self.jobs.result(job_id, self.context(), wait_seconds=10)
            if isinstance(result, BuildJobReport):
                return result
        raise AssertionError("build job did not finish")


def full_stack(tmp_path, monkeypatch, **kwargs):
    """The production build stack over a temporary allowed root (skips without the domain)."""
    pytest.importorskip("sonder_runtime.domain.build.templates",
                        reason="needs the build domain (lane A-domain-build)")
    if not HAVE_TOOLS:
        pytest.skip("cmake, ninja, make and g++ are required")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(allowed))
    stack = FullStack(tmp_path, **kwargs)
    stack.allowed = allowed
    return stack


@pytest.fixture
def full(tmp_path, monkeypatch):
    return full_stack(tmp_path, monkeypatch)


def request(root, **kwargs):
    from sonder_runtime.application.build.ports import BuildJobRequest

    return BuildJobRequest(project=str(root), **kwargs)


def configure(full, root, generator="Ninja", build="build/ninja"):
    job = full.jobs.start(request(root, build_dir=build, action="configure", generator=generator,
                                  config="Debug"), full.context())
    report = full.result(job)
    assert report.status == "succeeded", (report.notes, report.first_errors)
    return report


@pytest.mark.parametrize("generator,build", [("Ninja", "build/ninja"), ("Unix Makefiles", "build/make")])
def test_configure_then_build_attributes_the_seeded_error(full, generator, build):
    from sonder_runtime.application.build.ports import BuildModelRequest
    from sonder_runtime.domain.build.report import build_report_to_wire

    root = write_project(full.allowed / "mini")
    configure(full, root, generator, build)
    assert root.joinpath(build, *QUERY_RELATIVE).is_file()
    model = full.models.model(BuildModelRequest(project=str(root), build_dir=build), full.context())
    names = {target.name: target for target in model.targets}
    assert {"core", "game", "gen", "deploy"} <= set(names)
    assert names["gen"].build_time_tool and names["deploy"].utility
    assert model.source.value == "file_api" and model.compile_db_available
    report = full.result(full.jobs.start(request(root, build_dir=build, target="core"), full.context()))
    assert report.status == "failed" and report.exit_code not in (0, None)
    first = report.first_errors[0]
    assert (first.file, first.line, first.severity) == ("src/core/math.cpp", 4, "error")
    assert "lenght" in first.message
    tu = [item for item in report.attributions if item.kind == "tu"]
    assert tu and tu[0].label.endswith("src/core/math.cpp")
    assert str(full.allowed) not in json.dumps(build_report_to_wire(report))


def test_compile_one_uses_the_ninja_caret_target(full):
    root = write_project(full.allowed / "mini")
    configure(full, root)
    good = request(root, build_dir="build/ninja", action="compile_one", file="src/core/entity.cpp")
    plan = full.jobs.plan(good, full.context())
    assert plan.argv[-1].endswith("src/core/entity.cpp^") and plan.template_id == "ninja.compile_one"
    assert full.result(full.jobs.start(good, full.context())).status == "succeeded"
    failing = full.result(full.jobs.start(
        request(root, build_dir="build/ninja", action="compile_one", file="src/core/math.cpp"),
        full.context()))
    assert failing.status == "failed" and failing.first_errors[0].file == "src/core/math.cpp"


def test_compile_one_on_a_makefile_tree_falls_back_to_the_target(full):
    root = write_project(full.allowed / "mini")
    configure(full, root, "Unix Makefiles", "build/make")
    plan = full.jobs.plan(request(root, build_dir="build/make", action="compile_one",
                                  file="src/core/entity.cpp"), full.context())
    assert plan.template_id == "cmake.build" and "core" in plan.argv
    assert any("target build" in note for note in plan.notes)


def test_include_trace_is_sanitized_and_finds_the_pch_header(full):
    root = write_project(full.allowed / "mini")
    configure(full, root)
    trace = request(root, build_dir="build/ninja", action="include_trace", file="src/core/entity.cpp")
    plan = full.jobs.plan(trace, full.context())
    assert plan.template_id == "trace.gnu"
    assert "-H" in plan.argv and "-fsyntax-only" in plan.argv
    for bad in ("-o", "-MF", "-MD", "-c"):
        assert bad not in plan.display_argv
    report = full.result(full.jobs.start(trace, full.context()))
    headers = report.include_trace.headers()
    assert any(item.endswith("entity.h") for item in headers)
    assert any(item.endswith("pch.h") for item in headers)


def test_a_utility_target_is_refused_before_launch(full):
    root = write_project(full.allowed / "mini")
    configure(full, root)
    before = set(full.run_root.iterdir())
    with pytest.raises(SonderError) as excinfo:
        full.jobs.start(request(root, build_dir="build/ninja", target="deploy"), full.context())
    assert excinfo.value.code == "UTILITY_TARGET_REFUSED"
    assert set(full.run_root.iterdir()) == before


def _sleepers():
    found = []
    for pid in _pids_with_marker("600"):
        try:
            argv = Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")
        except OSError:
            continue
        if argv[:2] == [b"sleep", b"600"]:
            found.append(pid)
    return found


def test_cancel_a_slow_custom_target_leaves_no_process(full):
    root = write_project(full.allowed / "mini")
    configure(full, root)
    full.planner._utility_allow = frozenset({"slow"})
    before = set(_sleepers())
    job = full.jobs.start(request(root, build_dir="build/ninja", target="slow"), full.context())
    deadline = time.monotonic() + 60
    sleepers: list[int] = []
    while time.monotonic() < deadline and not sleepers:
        sleepers = [pid for pid in _sleepers() if pid not in before]
        time.sleep(0.1)
    assert sleepers
    view = full.jobs.cancel(job, full.context(), reason="stop")
    assert view.status == "cancelled" and view.cleanup_proven
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not _descendants_gone(sleepers):
        time.sleep(0.1)
    assert _descendants_gone(sleepers)
    assert full.result(job).status == "cancelled"


def test_a_deadline_reads_as_timed_out(full):
    root = write_project(full.allowed / "mini")
    configure(full, root)
    full.planner._utility_allow = frozenset({"slow"})
    plan = full.jobs.plan(request(root, build_dir="build/ninja", target="slow"), full.context())
    job = full.jobs.start(request(root), full.context(), plan=replace(plan, timeout_seconds=2))
    report = full.result(job, limit=120)
    assert report.status == "timed_out"


def test_an_env_dump_custom_command_sees_no_inherited_variables(full, monkeypatch):
    monkeypatch.setenv("SONDER_TEST_API_KEY", FAKE_SECRET)
    monkeypatch.setenv("OPERATOR_ONLY_VARIABLE", "leak")
    root = write_project(full.allowed / "mini")
    configure(full, root)
    full.planner._utility_allow = frozenset({"envdump"})
    job = full.jobs.start(request(root, build_dir="build/ninja", target="envdump"), full.context())
    full.result(job)
    log = Path(full.launcher.metadata(job)["log_file"]).read_text()
    assert FAKE_SECRET not in log and "OPERATOR_ONLY_VARIABLE" not in log
    assert "LC_ALL=C.UTF-8" in log


def test_gcc_and_clang_give_the_same_error_triples(full):
    if shutil.which("clang++") is None:
        pytest.skip("clang++ not installed")
    triples = {}
    for compiler in ("g++", "clang++"):
        root = write_project(full.allowed / ("mini-" + compiler.replace("+", "p")),
                             prefix="set(CMAKE_CXX_COMPILER %s)\n" % shutil.which(compiler))
        configure(full, root)
        report = full.result(full.jobs.start(request(root, build_dir="build/ninja", target="core"),
                                             full.context()))
        triples[compiler] = {(d.file, d.line, d.severity) for d in report.first_errors}
    assert triples["g++"] == triples["clang++"] == {("src/core/math.cpp", 4, "error")}
