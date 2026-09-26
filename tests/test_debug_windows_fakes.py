"""Windows paths exercised on any host: SDK discovery by arch, the Performance
Toolkit, the new registry specs, and the cdb/xperf plans with a fake inventory."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from sonder_runtime.adapters.host_tools import windows
from sonder_runtime.domain.host_tools.registry import spec_for
from tests.test_tools_inventory_fakes import FakeHost

KITS = "C:\\Program Files (x86)\\Windows Kits\\10\\"
SDK_KEY = "SOFTWARE\\Microsoft\\Windows Kits\\Installed Roots"
CDB = "C:\\Program Files (x86)\\Windows Kits\\10\\Debuggers\\x64\\cdb.exe"
XPERF = "C:\\Program Files (x86)\\Windows Kits\\10\\Windows Performance Toolkit\\xperf.exe"
SYMBOLIZER = "C:\\Program Files\\LLVM\\bin\\llvm-symbolizer.exe"
STACKWALK = "C:\\Users\\alice\\.cargo\\bin\\minidump-stackwalk.exe"
WINDOWS_APPS_CDB = "C:\\Users\\alice\\AppData\\Local\\Microsoft\\WindowsApps\\cdb.exe"


def _sdk_host(**env):
    host = FakeHost("Windows", path="C:\\Windows\\System32",
                    env={"ProgramFiles(x86)": "C:\\Program Files (x86)", **env},
                    home="C:\\Users\\alice")
    host.registry_values[("HKLM", SDK_KEY, "KitsRoot10")] = KITS
    host.registry_keys[("HKLM", SDK_KEY)] = ("10.0.22621.0",)
    for arch in ("x64", "x86", "arm64"):
        host.add_exe(KITS + "Debuggers\\%s\\cdb.exe" % arch, "8:%s" % arch)
        host.add_exe(KITS + "Debuggers\\%s\\windbg.exe" % arch, "9:%s" % arch)
    for exe in ("xperf", "wpaexporter", "wpr"):
        host.add_exe(KITS + "Windows Performance Toolkit\\%s.exe" % exe, "10:%s" % exe)
    return host


@pytest.mark.parametrize("env,arch", [
    ({"PROCESSOR_ARCHITECTURE": "AMD64"}, "x64"),
    ({"PROCESSOR_ARCHITECTURE": "ARM64"}, "arm64"),
    ({"PROCESSOR_ARCHITECTURE": "x86"}, "x86"),
    ({"PROCESSOR_ARCHITECTURE": "x86", "PROCESSOR_ARCHITEW6432": "AMD64"}, "x64"),
    ({}, "x64"),
])
def test_sdk_debuggers_follow_the_host_architecture(env, arch):
    host = _sdk_host(**env)
    records = {r.name: r for r in windows.discover_windows_sdk(host.probes())}
    assert records["cdb"].path == KITS + "Debuggers\\%s\\cdb.exe" % arch
    assert records["windbg"].path == KITS + "Debuggers\\%s\\windbg.exe" % arch
    assert dict(records["cdb"].details)["arch"] == arch
    if arch != "x86":
        assert records["cdb"].alternatives == (KITS + "Debuggers\\x86\\cdb.exe",)
    else:
        assert records["cdb"].alternatives == ()
    assert host.runs == []


def test_the_performance_toolkit_is_discovered_from_metadata():
    host = _sdk_host()
    records = {r.name: r for r in windows.discover_windows_sdk(host.probes())}
    for exe in ("xperf", "wpaexporter", "wpr"):
        assert records[exe].path == KITS + "Windows Performance Toolkit\\%s.exe" % exe
    assert host.runs == []


def test_registry_specs_for_the_digest_engines():
    symbolizer = spec_for("llvm-symbolizer")
    assert symbolizer.executables[0] == "llvm-symbolizer"
    assert {"llvm-symbolizer-18", "llvm-symbolizer-19", "llvm-symbolizer-20"} <= set(symbolizer.executables)
    assert spec_for("minidump-stackwalk").executables == ("minidump-stackwalk", "minidump_stackwalk")
    assert spec_for("eu-stack").platforms == frozenset({"Linux"})
    assert spec_for("heaptrack_print").version_args == ("-v",)
    assert spec_for("unshare").version_args == ("--version",)
    for name in ("callgrind_annotate", "tracy-csvexport", "symchk", "wpr", "procdump"):
        assert spec_for(name).version_args is None, name
    for name in ("symchk", "wpr", "procdump"):
        assert spec_for(name).platforms == frozenset({"Windows"})
    for name in ("gdb", "lldb", "valgrind", "perf", "cdb", "windbg", "heaptrack", "xperf", "wpaexporter"):
        assert spec_for(name) is not None, name


# -- plans with a fake inventory (system="Windows") ------------------------------------------

from sonder_runtime.adapters.debugging.planner import HostDebugPlanner  # noqa: E402
from sonder_runtime.application.context import local_owner_context  # noqa: E402
from sonder_runtime.application.debugging.ports import (  # noqa: E402
    CaptureIdentity,
    CrashDigestRequest,
    ProfileDigestRequest,
)
from sonder_runtime.domain.common.errors import SonderError  # noqa: E402
from sonder_runtime.domain.crash.model import CrashReport, ModuleInfo  # noqa: E402

CDB_COMMANDS = (
    ".echo SONDER_{nonce}_BEGIN;.ecxr;.echo SONDER_{nonce}_STACK;kn 64;.echo SONDER_{nonce}_ANALYZE;"
    "!analyze -v;.echo SONDER_{nonce}_THREADS;~*kn 16;.echo SONDER_{nonce}_MODULES;lm t n;"
    ".echo SONDER_{nonce}_END;q"
)


@dataclass(frozen=True)
class Record:
    name: str
    path: str
    version: str = "10.0.22621.0"
    identity: str = "1:1"


class FakeHostToolLookup:
    def __init__(self, tools=None):
        self.tools = dict(tools if tools is not None else {
            "cdb": CDB, "xperf": XPERF, "llvm-symbolizer": SYMBOLIZER, "minidump-stackwalk": STACKWALK,
        })

    def lookup(self, name):
        path = self.tools.get(name)
        return Record(name, path) if path else None


def planner(tools=None, *, drive=3, stores=(), **kwargs):
    return HostDebugPlanner(FakeHostToolLookup(tools), redact=lambda text: text, system="Windows",
                            isolation_probe=lambda path: True, stores=lambda: stores,
                            drive_type=lambda root: drive, system_root="C:\\Windows",
                            state_dir="C:\\Users\\alice\\AppData\\Local\\Sonder", **kwargs)


def dump_identity(kind="windows_minidump"):
    return CaptureIdentity(path="C:\\dumps\\game.dmp", label="game.dmp", size=4096, dev=1, ino=2,
                           mtime_ns=3, sha256="ab" * 32, kind=kind)


def tier0(*names):
    return CrashReport(source_kind="windows_minidump",
                       modules=tuple(ModuleInfo(name=name, path="C:\\x\\" + name) for name in names))


def ctx():
    return local_owner_context(correlation_id=uuid.uuid4().hex)


def plan_cdb(**kwargs):
    request = CrashDigestRequest("C:\\dumps\\game.dmp", engine="cdb",
                                 symbol_dirs=kwargs.pop("dirs", ()),
                                 symbol_server=kwargs.pop("symbol_server", False))
    judge = kwargs.pop("judge", None) or planner(**kwargs.pop("planner", {}))
    return judge.plan_crash(request, ctx(), network_allowed=kwargs.pop("network", False),
                            identity=dump_identity(), tier0=kwargs.pop("tier0", tier0("game.exe", "ntdll.dll")))


def test_cdb_argv_is_exact():
    plan = plan_cdb()
    (step,) = plan.steps
    assert step.template_argv == (
        CDB, "-z", "{input}", "-lines", "-noshell", "-sins", "-netsyms", "no", "-sflags", "0x022802B7",
        "-y", "{sympath}", "-i", "{imagepath}", "-c", CDB_COMMANDS)
    assert ".symopt+0x40" not in " ".join(step.template_argv)
    assert "kP" not in step.template_argv[-1] and "bt full" not in step.template_argv[-1]
    assert step.isolation == "none" and plan.egress_isolation == "none"
    bindings = dict(plan.bindings)
    assert bindings["sympath"] == "cache*{rundir}\\symcache"
    assert bindings["imagepath"] == "{rundir}\\img"
    assert step.memory_limit_bytes == 4 << 30 and step.timeout_seconds == 180


def test_cdb_environment_is_built_from_scratch():
    env = dict(plan_cdb().steps[0].environment)
    assert env["USERPROFILE"] == env["LOCALAPPDATA"] == env["APPDATA"] == "{rundir}\\home"
    assert env["TEMP"] == env["TMP"] == "{rundir}\\tmp"
    assert env["SystemRoot"] == env["windir"] == "C:\\Windows"
    assert env["PATH"] == "C:\\Program Files (x86)\\Windows Kits\\10\\Debuggers\\x64;C:\\Windows\\System32"
    assert env["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert not [key for key in env if key.startswith("_NT_")]
    for forbidden in ("INIT", "DBGHELP_LOG", "SRCSRV_INI_FILE"):
        assert forbidden not in env


def test_xperf_alone_gets_the_symbol_cache_variables():
    plan = planner().plan_profile(ProfileDigestRequest("C:\\t\\trace.etl"), ctx(),
                                  identity=dump_identity("etw_etl"))
    env = dict(plan.steps[0].environment)
    assert env["_NT_SYMCACHE_PATH"] == "{rundir}\\symcache"
    assert "_NT_SYMBOL_PATH" in env and env["_NT_SYMBOL_PATH"] == ""
    assert plan.steps[0].reads_output_via.startswith("file:")
    assert any("experimental" in note for note in plan.notes)


@pytest.mark.parametrize("module", ["coreclr.dll", "clr.dll", "mscorwks.dll", "CoreCLR.DLL"])
def test_managed_dumps_are_refused_for_cdb(module):
    with pytest.raises(SonderError) as caught:
        plan_cdb(tier0=tier0("game.exe", module))
    assert caught.value.code == "ENGINE_REFUSED_MANAGED_DUMP"


def test_an_unreadable_dump_is_not_handed_to_cdb():
    with pytest.raises(SonderError) as caught:
        plan_cdb(tier0=None)
    assert caught.value.code == "ENGINE_REFUSED_MANAGED_DUMP"


def test_auto_skips_cdb_on_a_managed_dump_and_walks_with_stackwalk():
    request = CrashDigestRequest("C:\\dumps\\game.dmp")
    plan = planner().plan_crash(request, ctx(), network_allowed=False, identity=dump_identity(),
                                tier0=tier0("game.exe", "coreclr.dll"))
    assert [step.engine for step in plan.steps] == ["minidump_stackwalk"]
    assert any("managed" in note for note in plan.notes)
    assert "--symbols-url" not in plan.steps[0].template_argv


@pytest.mark.parametrize("bad", [
    "\\\\buildserver\\symbols", "srv*C:\\cache*https://msdl.microsoft.com/download/symbols",
    "C:\\syms;D:\\more", "C:\\syms*", "https://symbols.example.com", "..\\syms", "C:\\a\\..\\b",
    "\\\\?\\C:\\syms", "syms", "C:\\syms\"",
])
def test_symbol_dirs_that_could_reach_the_network_are_rejected(bad):
    with pytest.raises(SonderError) as caught:
        plan_cdb(dirs=(bad,))
    assert caught.value.code == "SYMBOL_PATH_REJECTED"


def test_a_mapped_network_drive_is_rejected():
    with pytest.raises(SonderError) as caught:
        plan_cdb(dirs=("Z:\\builds\\syms",), planner={"drive": 4})
    assert caught.value.code == "SYMBOL_PATH_REJECTED"


def test_local_symbol_dirs_feed_the_symbol_and_image_paths():
    plan = plan_cdb(dirs=("C:\\build\\RelWithDebInfo", "D:\\engine\\pdb"))
    bindings = dict(plan.bindings)
    assert bindings["sympath"] == "cache*{rundir}\\symcache;C:\\build\\RelWithDebInfo;D:\\engine\\pdb"
    assert bindings["imagepath"] == "C:\\build\\RelWithDebInfo;D:\\engine\\pdb"
    assert "srv*" not in bindings["sympath"]


def test_operator_stores_appear_only_with_network():
    stores = ("\\\\buildserver\\symbols", "https://symbols.studio.example")
    offline = plan_cdb(planner={"stores": stores})
    assert "srv*" not in dict(offline.bindings)["sympath"] and offline.stores_display == ()
    online = plan_cdb(planner={"stores": stores}, network=True, symbol_server=True)
    sympath = dict(online.bindings)["sympath"]
    assert "*https://msdl.microsoft.com/download/symbols" in sympath
    assert "*\\\\buildserver\\symbols" in sympath and "*https://symbols.studio.example" in sympath
    assert online.stores_display == stores and online.network is True
    assert online.steps[0].template_argv[7] == "yes"
    assert online.command_digest != offline.command_digest


def test_a_malformed_operator_store_is_rejected():
    with pytest.raises(SonderError) as caught:
        plan_cdb(planner={"stores": ("http://plain.example/symbols",)}, network=True,
                 symbol_server=True)
    assert caught.value.code == "SYMBOL_STORE_REJECTED"


def test_a_store_cannot_come_from_tool_arguments():
    fields = set(CrashDigestRequest.__dataclass_fields__)
    assert not fields & {"stores", "store", "symbol_path", "argv", "env"}


def test_network_needs_both_the_request_and_the_service_decision():
    plan = plan_cdb(network=True)  # the service allowed it, but the request did not ask
    assert plan.network is False


@pytest.mark.parametrize("engine", ["gdb", "eu_stack", "lldb"])
def test_linux_debuggers_are_refused_for_cores_on_windows(engine):
    request = CrashDigestRequest("C:\\dumps\\core", engine=engine, executable="")
    with pytest.raises(SonderError) as caught:
        planner().plan_crash(request, ctx(), network_allowed=False, identity=dump_identity("elf_core"),
                             tier0=None)
    assert caught.value.code == "ENGINE_UNSUPPORTED_ON_PLATFORM"


@pytest.mark.parametrize("kind,engine", [("perf_data", "auto"), ("heaptrack_capture", "auto")])
def test_linux_profilers_are_refused_on_windows(kind, engine):
    with pytest.raises(SonderError) as caught:
        planner({"perf": "C:\\perf.exe", "heaptrack_print": "C:\\h.exe"}).plan_profile(
            ProfileDigestRequest("C:\\p", engine=engine), ctx(), identity=dump_identity(kind))
    assert caught.value.code == "ENGINE_UNSUPPORTED_ON_PLATFORM"


def test_the_store_windbg_cdb_is_refused():
    with pytest.raises(SonderError) as caught:
        plan_cdb(planner={"tools": {"cdb": WINDOWS_APPS_CDB}})
    assert caught.value.code == "ENGINE_UNAVAILABLE"
    assert "WindowsApps" in str(caught.value)


def test_cdb_is_unavailable_when_not_installed():
    with pytest.raises(SonderError) as caught:
        plan_cdb(planner={"tools": {}})
    assert caught.value.code == "ENGINE_UNAVAILABLE"


def test_wpaexporter_is_refused_until_its_profile_ships(tmp_path):
    with pytest.raises(SonderError) as caught:
        planner({"wpaexporter": "C:\\wpa.exe"}, profiles_dir=tmp_path).plan_profile(
            ProfileDigestRequest("C:\\t.etl", engine="wpaexporter"), ctx(), identity=dump_identity("etw_etl"))
    assert caught.value.code == "ENGINE_UNAVAILABLE"
