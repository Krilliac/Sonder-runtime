"""HostDebugPlanner: engine selection by kind x platform x inventory, refusals,
isolation, bounds and approval-digest stability (Linux host, real lane A)."""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from sonder_runtime.adapters.debugging.capture_source import GuardedCaptureSource  # noqa: E402
from sonder_runtime.adapters.debugging.planner import (  # noqa: E402
    HostDebugPlanner,
    posix_debugger_memory,
)
from sonder_runtime.adapters.debugging.triage import PureCaptureTriage  # noqa: E402
from sonder_runtime.application.context import local_owner_context  # noqa: E402
from sonder_runtime.application.debugging.ports import (  # noqa: E402
    CaptureIdentity,
    CrashDigestRequest,
    ProfileDigestRequest,
)
from sonder_runtime.domain.common.errors import InvalidInput, SonderError  # noqa: E402
from sonder_runtime.domain.crash.model import (  # noqa: E402
    CrashReport,
    ModuleInfo,
    StackFrame,
    ThreadSummary,
)

GIB = 1 << 30


@dataclass(frozen=True)
class Record:
    name: str
    path: str
    version: str = "1.0"
    identity: str = "7:7"


class Lookup:
    def __init__(self, tools):
        self.tools = tools

    def lookup(self, name):
        path = self.tools.get(name)
        return Record(name, path) if path else None


LINUX_TOOLS = {"gdb": "/usr/bin/gdb", "lldb": "/usr/bin/lldb", "eu-stack": "/usr/bin/eu-stack",
               "unshare": "/usr/bin/unshare", "perf": "/usr/bin/perf",
               "heaptrack_print": "/usr/bin/heaptrack_print",
               "tracy-csvexport": "/opt/tracy/csvexport", "llvm-symbolizer": "/usr/bin/llvm-symbolizer"}


@pytest.fixture
def root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(allowed))
    return allowed


def planner(tools=None, *, probe=True, system="Linux", **kwargs):
    return HostDebugPlanner(Lookup(LINUX_TOOLS if tools is None else tools), redact=lambda t: t,
                            system=system, isolation_probe=(lambda path: probe),
                            source=GuardedCaptureSource(), **kwargs)


def ctx():
    return local_owner_context(correlation_id=uuid.uuid4().hex)


def core_identity(size=1000, kind="elf_core"):
    return CaptureIdentity("/w/core", "core", size, 1, 2, 3, "cd" * 32, kind)


def exe(root) -> str:
    path = root / "game"
    path.write_bytes(b"\x7fELF" + b"\x00" * 60)
    return str(path)


# -- cores ----------------------------------------------------------------------------------


@pytest.mark.parametrize("engine,first", [("auto", "gdb"), ("gdb", "gdb"), ("lldb", "lldb"),
                                          ("eu_stack", "eu_stack")])
def test_core_engines_on_linux(root, engine, first):
    plan = planner().plan_crash(CrashDigestRequest("/w/core", executable=exe(root), engine=engine),
                                ctx(), network_allowed=False, identity=core_identity(), tier0=None)
    (step,) = plan.steps
    assert step.engine == first and plan.engines == ("pure", first)
    assert step.template_argv[:5] == ("/usr/bin/unshare", "--user", "--map-current-user", "--net", "--")
    assert step.isolation == "netns" and plan.egress_isolation == "netns"
    assert dict(plan.bindings)["exe"] == str(root / "game")
    assert "/usr/bin/unshare" in plan.checked_executables
    env = dict(step.environment)
    assert env["DEBUGINFOD_URLS"] == "" and env["HOME"] == "{rundir}/home"
    assert env["PATH"] == "/usr/bin:/bin" and env["TMPDIR"] == "{rundir}/tmp"
    assert not [key for key in env if key.startswith(("LD_", "PYTHON", "LLDB_", "GDBHIST"))]


def test_auto_falls_back_through_the_installed_debuggers(root):
    tools = dict(LINUX_TOOLS)
    del tools["gdb"]
    plan = planner(tools).plan_crash(CrashDigestRequest("/w/core", executable=exe(root)), ctx(),
                                     network_allowed=False, identity=core_identity(), tier0=None)
    assert plan.steps[0].engine == "lldb"


def test_an_explicit_missing_engine_is_unavailable_with_an_install_hint(root):
    tools = dict(LINUX_TOOLS)
    del tools["eu-stack"]
    with pytest.raises(SonderError) as caught:
        planner(tools).plan_crash(CrashDigestRequest("/w/core", executable=exe(root), engine="eu_stack"),
                                  ctx(), network_allowed=False, identity=core_identity(), tier0=None)
    assert caught.value.code == "ENGINE_UNAVAILABLE" and "elfutils" in str(caught.value)


def test_cores_need_the_executable(root):
    with pytest.raises(SonderError) as caught:
        planner().plan_crash(CrashDigestRequest("/w/core", engine="gdb"), ctx(),
                             network_allowed=False, identity=core_identity(), tier0=None)
    assert caught.value.code == "EXECUTABLE_REQUIRED"
    plan = planner().plan_crash(CrashDigestRequest("/w/core"), ctx(), network_allowed=False,
                                identity=core_identity(), tier0=None)
    assert plan.steps == () and any("EXECUTABLE_REQUIRED" in note for note in plan.notes)


def test_an_executable_outside_the_roots_is_refused(root, tmp_path):
    outside = tmp_path / "evil"
    outside.write_bytes(b"\x7fELF")
    with pytest.raises(SonderError) as caught:
        planner().plan_crash(CrashDigestRequest("/w/core", executable=str(outside), engine="gdb"),
                             ctx(), network_allowed=False, identity=core_identity(), tier0=None)
    assert caught.value.code == "CAPTURE_REJECTED"


def test_an_executable_spelling_a_placeholder_is_refused(root):
    """The launcher rewrites ``{rundir}`` in bindings; a host path must never carry one."""
    path = root / "game{rundir}"
    path.write_bytes(b"\x7fELF" + b"\x00" * 60)
    with pytest.raises(SonderError) as caught:
        planner().plan_crash(CrashDigestRequest("/w/core", executable=str(path), engine="gdb"),
                             ctx(), network_allowed=False, identity=core_identity(), tier0=None)
    assert caught.value.code == "INVALID_INPUT" or isinstance(caught.value, InvalidInput)


def test_no_netns_when_the_probe_fails_or_unshare_is_missing(root):
    for tools, probe in ((LINUX_TOOLS, False), ({k: v for k, v in LINUX_TOOLS.items() if k != "unshare"}, True)):
        plan = planner(tools, probe=probe).plan_crash(
            CrashDigestRequest("/w/core", executable=exe(root)), ctx(), network_allowed=False,
            identity=core_identity(), tier0=None)
        assert plan.steps[0].template_argv[0] == "/usr/bin/gdb"
        assert plan.egress_isolation == "none"


def test_network_turns_debuginfod_on_and_drops_the_namespace(root):
    plan = planner(distro="ubuntu").plan_crash(
        CrashDigestRequest("/w/core", executable=exe(root), symbol_server=True), ctx(),
        network_allowed=True, identity=core_identity(), tier0=None)
    step = plan.steps[0]
    assert step.template_argv[0] == "/usr/bin/gdb" and step.isolation == "none"
    assert "set debuginfod enabled on" in step.template_argv
    assert dict(step.environment)["DEBUGINFOD_URLS"] == "https://debuginfod.ubuntu.com"


def test_posix_debugger_memory_scales_with_the_core():
    assert posix_debugger_memory(0) == 4 * GIB
    assert posix_debugger_memory(500 << 20) == 4 * GIB
    assert posix_debugger_memory(3 * GIB) == 6 * GIB
    assert posix_debugger_memory(64 * GIB) == 24 * GIB


def test_the_plan_carries_the_scaled_limit_and_bounded_timeouts(root):
    plan = planner().plan_crash(CrashDigestRequest("/w/core", executable=exe(root), timeout_seconds=5),
                                ctx(), network_allowed=False, identity=core_identity(size=5 * GIB),
                                tier0=None)
    step = plan.steps[0]
    assert step.memory_limit_bytes == 8 * GIB and step.timeout_seconds == 10
    assert step.max_output_bytes == 16 << 20
    plan = planner().plan_crash(CrashDigestRequest("/w/core", executable=exe(root), timeout_seconds=5000),
                                ctx(), network_allowed=False, identity=core_identity(), tier0=None)
    assert plan.steps[0].timeout_seconds == 900


@pytest.mark.parametrize("engine,code", [("cdb", "ENGINE_UNAVAILABLE"),
                                         ("minidump_stackwalk", "ENGINE_UNAVAILABLE")])
def test_minidump_only_engines_refuse_cores(root, engine, code):
    with pytest.raises(SonderError) as caught:
        planner().plan_crash(CrashDigestRequest("/w/core", executable=exe(root), engine=engine), ctx(),
                             network_allowed=False, identity=core_identity(), tier0=None)
    assert caught.value.code == code


def test_cdb_is_windows_only():
    with pytest.raises(SonderError) as caught:
        planner().plan_crash(CrashDigestRequest("/w/a.dmp", engine="cdb"), ctx(), network_allowed=False,
                             identity=core_identity(kind="windows_minidump"), tier0=None)
    assert caught.value.code == "ENGINE_UNSUPPORTED_ON_PLATFORM"


def test_macos_reads_cores_with_lldb_only(root):
    tools = dict(LINUX_TOOLS)
    plan = planner(tools, system="Darwin").plan_crash(
        CrashDigestRequest("/w/core", executable=exe(root)), ctx(), network_allowed=False,
        identity=core_identity(), tier0=None)
    assert plan.steps[0].engine == "lldb" and plan.egress_isolation == "none"
    with pytest.raises(SonderError) as caught:
        planner(tools, system="Darwin").plan_crash(
            CrashDigestRequest("/w/core", executable=exe(root), engine="gdb"), ctx(),
            network_allowed=False, identity=core_identity(), tier0=None)
    assert caught.value.code == "ENGINE_UNSUPPORTED_ON_PLATFORM"


@pytest.mark.parametrize("kind", ["sanitizer_report", "valgrind_xml", "apple_ips"])
def test_text_captures_are_pure_only(kind):
    plan = planner().plan_crash(CrashDigestRequest("/w/x"), ctx(), network_allowed=False,
                                identity=core_identity(kind=kind), tier0=None)
    assert plan.steps == ()
    with pytest.raises(SonderError):
        planner().plan_crash(CrashDigestRequest("/w/x", engine="gdb"), ctx(), network_allowed=False,
                             identity=core_identity(kind=kind), tier0=None)


def test_engine_pure_plans_nothing(root):
    plan = planner().plan_crash(CrashDigestRequest("/w/core", executable=exe(root), engine="pure"),
                                ctx(), network_allowed=False, identity=core_identity(), tier0=None)
    assert plan.steps == () and plan.egress_isolation == "n/a"


# -- approval binding ------------------------------------------------------------------------


def test_two_plans_of_the_same_request_have_the_same_digest_and_no_run_values(root):
    capture = root / "core.1"
    capture.write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + b"\x04\x00" + b"\x00" * 100)
    source = GuardedCaptureSource()
    request = CrashDigestRequest(str(capture), executable=exe(root))

    def plan_once():
        reader, ident = source.open_reader(str(capture))
        reader.close()
        return planner().plan_crash(request, ctx(), network_allowed=False, identity=ident, tier0=None)

    first, second = plan_once(), plan_once()
    assert first.command_digest == second.command_digest
    assert first.resolved_command() == second.resolved_command()
    rendered = repr(first.resolved_command())
    assert "{nonce}" in rendered and "{input}" in rendered
    assert str(root) not in repr(first.resolved_command()["display_argvs"])
    capture.write_bytes(capture.read_bytes() + b"changed")
    os.utime(capture, ns=(1, 1))
    assert plan_once().command_digest != first.command_digest


def test_the_digest_binds_engine_executable_and_symbol_dirs(root):
    syms = root / "syms"
    syms.mkdir()
    other = root / "game2"
    other.write_bytes(b"\x7fELF" + b"\x01" * 60)
    base = CrashDigestRequest("/w/core", executable=exe(root))

    def digest(request):
        return planner().plan_crash(request, ctx(), network_allowed=False, identity=core_identity(),
                                    tier0=None).command_digest

    reference = digest(base)
    assert digest(CrashDigestRequest("/w/core", executable=exe(root), engine="lldb")) != reference
    assert digest(CrashDigestRequest("/w/core", executable=str(other))) != reference
    assert digest(CrashDigestRequest("/w/core", executable=exe(root), symbol_dirs=(str(syms),))) != reference


def test_symbol_dirs_are_contained(root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = root / "linked"
    link.symlink_to(outside)
    for bad in (str(outside), str(link), str(root / "missing"), "relative/syms", str(root) + "/a:b"):
        with pytest.raises(SonderError) as caught:
            planner().plan_crash(CrashDigestRequest("/w/core", executable=exe(root), symbol_dirs=(bad,)),
                                 ctx(), network_allowed=False, identity=core_identity(), tier0=None)
        assert caught.value.code == "SYMBOL_PATH_REJECTED", bad
    with pytest.raises(SonderError):
        planner().plan_crash(CrashDigestRequest("/w/core", symbol_dirs=tuple(str(root) for _ in range(9))),
                             ctx(), network_allowed=False, identity=core_identity(), tier0=None)


# -- profiles ---------------------------------------------------------------------------------


def test_perf_plans_both_reports_with_a_private_config():
    plan = planner().plan_profile(ProfileDigestRequest("/w/perf.data"), ctx(),
                                  identity=core_identity(kind="perf_data"))
    assert [step.parser for step in plan.steps] == ["perf_folded", "perf_flat"]
    for step in plan.steps:
        env = dict(step.environment)
        assert env["PERF_CONFIG"] == "/dev/null" and env["PERF_BUILDID_DIR"] == "{rundir}/buildid"
        assert step.memory_limit_bytes == 2 * GIB and step.isolation == "netns"
        assert "script" not in step.template_argv and "--tui" not in step.template_argv
    assert "buildid" in plan.mkdirs


def test_profile_tools_missing_or_mismatched():
    with pytest.raises(SonderError) as caught:
        planner({}).plan_profile(ProfileDigestRequest("/w/perf.data"), ctx(),
                                 identity=core_identity(kind="perf_data"))
    assert caught.value.code == "ENGINE_UNAVAILABLE" and "cpu-clock" in str(caught.value)
    with pytest.raises(SonderError):
        planner().plan_profile(ProfileDigestRequest("/w/perf.data", engine="xperf"), ctx(),
                               identity=core_identity(kind="perf_data"))


def test_etl_on_linux_is_refused_with_a_hint():
    with pytest.raises(SonderError) as caught:
        planner().plan_profile(ProfileDigestRequest("/w/trace.etl"), ctx(),
                               identity=core_identity(kind="etw_etl"))
    assert caught.value.code == "ENGINE_UNSUPPORTED_ON_PLATFORM" and "WPA" in str(caught.value)


def test_tracy_adds_the_unwrap_step_only_for_a_frame_zone():
    plain = planner().plan_profile(ProfileDigestRequest("/w/c.tracy"), ctx(),
                                   identity=core_identity(kind="tracy_capture"))
    zoned = planner().plan_profile(ProfileDigestRequest("/w/c.tracy", frame_zone="Frame"), ctx(),
                                   identity=core_identity(kind="tracy_capture"))
    assert len(plain.steps) == 1 and len(zoned.steps) == 2 and "-u" in zoned.steps[1].template_argv


def test_pure_profiles_plan_no_steps():
    plan = planner().plan_profile(ProfileDigestRequest("/w/callgrind.out.1"), ctx(),
                                  identity=core_identity(kind="callgrind"))
    assert plan.steps == ()


# -- verified PE+PDB pairs for llvm-symbolizer ---------------------------------------------------

TINY_C = r"""
__declspec(noinline) int helper(int value) {
    return value * 3 + HELPER_BIAS;
}

int main(void) {
    volatile int *pointer = 0;
    return helper(*pointer);
}
"""


def _build_pe(out: Path, bias: int, pdb_alt: str) -> None:
    out.mkdir(parents=True)
    source = out / "spark_tiny.c"
    source.write_text(TINY_C)
    subprocess.run(["clang-cl-18", "--target=x86_64-pc-windows-msvc", "/Z7", "/O1", "/GS-", "/c",
                    "-DHELPER_BIAS=%d" % bias, "/Fo" + str(out / "spark_tiny.obj"), "--", str(source)],
                   check=True, capture_output=True, cwd=out, timeout=120)
    subprocess.run(["lld-link-18", "/debug", "/entry:main", "/subsystem:console", "/nodefaultlib",
                    str(out / "spark_tiny.obj"), "/out:" + str(out / "spark_tiny.exe"),
                    "/pdb:" + str(out / "spark_tiny.pdb"), "/pdbaltpath:" + pdb_alt],
                   check=True, capture_output=True, cwd=out, timeout=120)


@pytest.fixture(scope="module")
def pe_pairs(tmp_path_factory):
    if not (shutil.which("clang-cl-18") and shutil.which("lld-link-18")):
        pytest.skip("clang-cl-18/lld-link-18 are not installed")
    base = tmp_path_factory.mktemp("pe")
    _build_pe(base / "a", 1, "C:\\build\\out\\spark_tiny.pdb")
    _build_pe(base / "b", 7, "C:\\build\\out\\spark_tiny.pdb")
    return base


def _dump_for(image: Path) -> bytes:
    from tests.support import minidump_builder as builder_module
    from sonder_runtime.domain.binaries.pe_debug import read_pe_identity
    from sonder_runtime.domain.binaries.reader import BytesReader

    pe = read_pe_identity(BytesReader(image.read_bytes()))
    base = 0x7FF6_1000_0000
    sp = 0xC1_2F8F_0100
    builder = builder_module.MinidumpBuilder().system_info(builder_module.AMD64, builder_module.WIN32NT)
    builder.module("C:\\build\\out\\spark_tiny.exe", base, 0x4000,
                   cv=builder_module.rsds_record(pe.rsds_guid, pe.rsds_age, pe.pdb_path))
    builder.thread(0x10, pc=base + 0x1003, sp=sp,
                   stack=builder_module.stack_with_returns(sp, [base + 0x1016]))
    builder.exception(0x10, 0xC0000005, address=base + 0x1003, params=(0, 0), pc=base + 0x1003, sp=sp)
    return builder.build()


def _minidump_plan(root, pe_pairs, exe_from: str, pdb_from: str, engine="llvm_symbolizer", tools=None):
    syms = root / "syms"
    syms.mkdir(exist_ok=True)
    shutil.copy(pe_pairs / exe_from / "spark_tiny.exe", syms / "spark_tiny.exe")
    shutil.copy(pe_pairs / pdb_from / "spark_tiny.pdb", syms / "spark_tiny.pdb")
    dump = root / "tiny.dmp"
    dump.write_bytes(_dump_for(pe_pairs / "a" / "spark_tiny.exe"))
    source = GuardedCaptureSource()
    reader, ident = source.open_reader(str(dump))
    try:
        base = PureCaptureTriage().crash(ident, reader)
    finally:
        reader.close()
    request = CrashDigestRequest(str(dump), symbol_dirs=(str(syms),), engine=engine)
    return planner(tools).plan_crash(request, ctx(), network_allowed=False, identity=ident, tier0=base), syms


def test_a_verified_pe_pdb_pair_is_staged_side_by_side(root, pe_pairs):
    plan, syms = _minidump_plan(root, pe_pairs, "a", "a")
    (step,) = plan.steps
    assert step.engine == "llvm_symbolizer" and plan.verified_modules == ("spark_tiny.exe",)
    assert "--no-debuginfod" in step.template_argv
    assert "{rundir}/sym/spark_tiny/spark_tiny.exe" in step.template_argv
    assert "0x1003" in step.template_argv
    staged = dict((dest, src) for src, dest in plan.staged_files)
    assert staged["sym/spark_tiny/spark_tiny.exe"] == str(syms / "spark_tiny.exe")
    assert staged["sym/spark_tiny/spark_tiny.pdb"] == str(syms / "spark_tiny.pdb")
    assert dict(step.environment)["DEBUGINFOD_URLS"] == ""


def test_a_mismatched_pdb_gives_symbols_mismatch_and_no_step(root, pe_pairs):
    plan, _ = _minidump_plan(root, pe_pairs, "a", "b")
    assert plan.steps == () and plan.module_symbols == (("spark_tiny.exe", "mismatch"),)
    assert any("PDB_MISMATCH" in note for note in plan.notes)


def test_an_image_from_another_build_is_not_symbolized(root, pe_pairs):
    plan, _ = _minidump_plan(root, pe_pairs, "b", "b")
    assert plan.steps == () and plan.module_symbols == (("spark_tiny.exe", "mismatch"),)


def test_stackwalk_gets_a_dump_syms_prestep_per_verified_module(root, pe_pairs):
    tools = dict(LINUX_TOOLS, **{"minidump-stackwalk": "/opt/cargo/minidump-stackwalk",
                                 "dump_syms": "/opt/cargo/dump_syms"})
    plan, _ = _minidump_plan(root, pe_pairs, "a", "a", engine="auto", tools=tools)
    assert [step.parser for step in plan.steps] == ["dump_syms", "stackwalk_json"]
    walk = plan.steps[1].template_argv
    assert "--symbols-path" in walk and "--symbols-url" not in walk and "--json" in walk
    assert any(rel.startswith("syms/spark_tiny.pdb/") for rel in plan.mkdirs)
