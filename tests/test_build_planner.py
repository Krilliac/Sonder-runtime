"""ProjectBuildPlanner without processes: MSBuild argv on a Windows-shaped
host, CMake Visual Studio trees, refusals, network downgrade, digests."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("sonder_runtime.domain.build.templates",
                    reason="needs the build domain (lane A-domain-build)")

from sonder_runtime.adapters.build.network import NetworkIsolation  # noqa: E402
from sonder_runtime.adapters.build.planner import ProjectBuildPlanner  # noqa: E402
from sonder_runtime.adapters.build.tree_reader import GuardedBuildTreeReader  # noqa: E402
from sonder_runtime.application.build.ports import (  # noqa: E402
    BuildEnvironment,
    BuildJobRequest,
    BuildModelRequest,
)
from sonder_runtime.application.context import local_owner_context  # noqa: E402
from sonder_runtime.domain.common.errors import SonderError  # noqa: E402

pytestmark = pytest.mark.unit

MSBUILD = r"C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe"

SLN = """
Microsoft Visual Studio Solution File, Format Version 12.00
Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "Engine", "Engine", "{0B6F1A10-0000-4000-8000-000000000001}"
EndProject
Project("{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}") = "SparkLite.Core", "src\\core\\core.vcxproj", "{0B6F1A10-0000-4000-8000-000000000002}"
EndProject
Project("{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}") = "Tools", "tools\\tools.vcxproj", "{0B6F1A10-0000-4000-8000-000000000004}"
EndProject
Global
	GlobalSection(SolutionConfigurationPlatforms) = preSolution
		Debug|x64 = Debug|x64
		Debug|Gaming.Xbox.Scarlett.x64 = Debug|Gaming.Xbox.Scarlett.x64
	EndGlobalSection
	GlobalSection(NestedProjects) = preSolution
		{0B6F1A10-0000-4000-8000-000000000002} = {0B6F1A10-0000-4000-8000-000000000001}
	EndGlobalSection
EndGlobal
"""
CORE = """<?xml version="1.0" encoding="utf-8"?>
<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup Label="ProjectConfigurations">
    <ProjectConfiguration Include="Debug|x64"><Configuration>Debug</Configuration><Platform>x64</Platform></ProjectConfiguration>
    <ProjectConfiguration Include="Debug|Gaming.Xbox.Scarlett.x64"><Configuration>Debug</Configuration><Platform>Gaming.Xbox.Scarlett.x64</Platform></ProjectConfiguration>
  </ItemGroup>
  <PropertyGroup Label="Configuration"><ConfigurationType>StaticLibrary</ConfigurationType><PlatformToolset>v143</PlatformToolset></PropertyGroup>
  <ImportGroup Label="PropertySheets"><Import Project="..\\..\\props\\engine.props" /></ImportGroup>
  <ItemGroup><ClCompile Include="math.cpp" /><ClCompile Include="entity.cpp" /></ItemGroup>
</Project>
"""
TOOLS = """<?xml version="1.0" encoding="utf-8"?>
<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemGroup Label="ProjectConfigurations">
    <ProjectConfiguration Include="Debug|x64"><Configuration>Debug</Configuration><Platform>x64</Platform></ProjectConfiguration>
  </ItemGroup>
  <PropertyGroup Label="Configuration"><ConfigurationType>Makefile</ConfigurationType></PropertyGroup>
</Project>
"""
PROPS = """<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemDefinitionGroup><ClCompile><PrecompiledHeader>Use</PrecompiledHeader>
  <PrecompiledHeaderFile>pch.h</PrecompiledHeaderFile></ClCompile></ItemDefinitionGroup>
</Project>
"""


@dataclass(frozen=True)
class Record:
    name: str
    path: str
    version: str = ""
    details: tuple = ()


class Lookup:
    def __init__(self, tools):
        self.tools = tools

    def lookup(self, name):
        path = self.tools.get(name)
        return None if path is None else Record(name, path)


class Env:
    def __init__(self):
        self.calls = []

    def environment(self, *, system, family, toolchain_hint="", arch="x64"):
        self.calls.append((system, family))
        return BuildEnvironment(pairs=(("PATH", "/usr/bin"),), keys=("PATH",), source="scrubbed")


class Net:
    def __init__(self, policy="enforced_off"):
        self.policy = policy
        self.launchers = []

    def decide(self, *, allow_network, launchers=()):
        self.launchers.append(tuple(launchers))
        if allow_network:
            return SimpleNamespace(policy="allowed", prefix=(), notes=(), checked_executables=())
        if self.policy != "enforced_off":
            return SimpleNamespace(policy=self.policy, prefix=(), notes=(), checked_executables=())
        return SimpleNamespace(policy=self.policy, prefix=("/usr/bin/unshare", "-rn", "--"),
                               notes=(), checked_executables=("/usr/bin/unshare",))


def ctx():
    return local_owner_context(correlation_id=uuid.uuid4().hex)


def write(path: Path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if isinstance(text, str) else json.dumps(text))
    return path


@pytest.fixture
def allowed(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    return root


def planner(tmp_path, *, host="linux", tools=None, network=None, env=None, **kwargs):
    network = network or Net("advisory_off" if host == "windows" else "enforced_off")
    tools = tools if tools is not None else {"cmake": "/usr/bin/cmake", "ninja": "/usr/bin/ninja",
                                             "make": "/usr/bin/make", "g++": "/usr/bin/g++"}
    return ProjectBuildPlanner(Lookup(tools), GuardedBuildTreeReader(), env or Env(),
                               network or Net(), run_root=str(tmp_path / "state" / "build-runs"),
                               host=host, executable_guard=lambda path: path, **kwargs)


@pytest.fixture
def solution(allowed):
    root = allowed / "eng"
    write(root / "SparkLite.sln", SLN)
    write(root / "src/core/core.vcxproj", CORE)
    write(root / "tools/tools.vcxproj", TOOLS)
    write(root / "props/engine.props", PROPS)
    return root


def plan(p, root, **kwargs):
    request = BuildJobRequest(project=str(root), **kwargs)
    model = p.plan_model(request.model_request(), ctx())
    return p.plan_run(request, model, ctx()), model


def test_msbuild_is_runner_unavailable_off_windows(tmp_path, solution):
    p = planner(tmp_path)
    model = p.plan_model(BuildModelRequest(project=str(solution)), ctx())
    assert model.system.value == "msbuild"
    assert "Gaming.Xbox.Scarlett.x64" in model.platforms
    with pytest.raises(SonderError) as excinfo:
        p.plan_run(BuildJobRequest(project=str(solution)), model, ctx())
    assert excinfo.value.code == "RUNNER_UNAVAILABLE"


def test_msbuild_sln_argv_with_mangled_target_and_custom_platform(tmp_path, solution):
    p = planner(tmp_path, host="windows", tools={"msbuild": MSBUILD})
    model = p.plan_model(BuildModelRequest(project=str(solution)), ctx())
    names = {target.name: target for target in model.targets}
    core = next(name for name in names if name.endswith("SparkLite_Core"))
    assert names["Tools"].utility  # a Makefile project
    result, _ = plan(p, solution, target=core, config="Debug", platform="Gaming.Xbox.Scarlett.x64")
    argv = result.argv
    assert argv[0] == MSBUILD and argv[1].endswith("SparkLite.sln")
    assert "-t:%s" % core in argv
    assert "-p:Platform=Gaming.Xbox.Scarlett.x64" in argv and "-p:Configuration=Debug" in argv
    assert "-p:VcpkgManifestInstall=false" in argv and "-p:RestorePackages=false" in argv
    flp = next(item for item in argv if item.startswith("-flp:LogFile="))
    assert flp.startswith("-flp:LogFile=" + result.log_dir)
    assert any(item.startswith("-bl:") for item in argv) and result.binlog.endswith(".binlog")
    assert result.extra_logs and "{log_file}" in " ".join(result.display_argv)
    assert result.template_id == "msbuild.build" and result.platform == "Gaming.Xbox.Scarlett.x64"


def test_msbuild_refusals(tmp_path, solution):
    p = planner(tmp_path, host="windows", tools={"msbuild": MSBUILD})
    for kwargs, code in (
        ({"target": "Tools"}, "UTILITY_TARGET_REFUSED"),
        ({"target": "Rebuild"}, "UNKNOWN_TARGET"),
        ({"platform": "PS9"}, "UNKNOWN_PLATFORM"),
        ({"target": "Nope"}, "UNKNOWN_TARGET"),
        ({"action": "configure"}, "ACTION_UNSUPPORTED"),
    ):
        with pytest.raises(SonderError) as excinfo:
            plan(p, solution, **kwargs)
        assert excinfo.value.code == code, kwargs


def test_msbuild_clcompile_on_the_owning_project(tmp_path, solution):
    p = planner(tmp_path, host="windows", tools={"msbuild": MSBUILD})
    result, _ = plan(p, solution, action="compile_one", file="src/core/math.cpp", config="Debug",
                     platform="x64")
    assert result.template_id == "msbuild.compile_one"
    assert result.argv[1].endswith(os.path.join("src", "core", "core.vcxproj"))
    assert "-t:ClCompile" in result.argv and "-p:SelectedFiles=math.cpp" in result.argv


def test_msbuild_include_trace_is_runner_unavailable(tmp_path, solution):
    p = planner(tmp_path, host="windows", tools={"msbuild": MSBUILD})
    with pytest.raises(SonderError) as excinfo:
        plan(p, solution, action="include_trace", file="src/core/math.cpp")
    assert excinfo.value.code == "RUNNER_UNAVAILABLE"


def vs_generator_tree(allowed):
    """A CMake Visual Studio generator tree: File API reply plus generated projects."""
    root = allowed / "vsgen"
    write(root / "CMakeLists.txt", "project(x)\n")
    write(root / "src/a.cpp", "int a;\n")
    build = root / "out" / "vs"
    reply = build / ".cmake/api/v1/reply"
    write(reply / "index-2026-01-01T00-00-00-0000.json", {
        "cmake": {"version": {"string": "3.28.3"}, "generator": {"name": "Visual Studio 17 2022",
                                                                 "multiConfig": True}},
        "objects": [{"kind": "codemodel", "version": {"major": 2, "minor": 6},
                     "jsonFile": "codemodel-v2-1.json"}],
        "reply": {"client-vs": {"query.json": {"responses": [
            {"kind": "codemodel", "version": {"major": 2, "minor": 6}, "jsonFile": "codemodel-v2-1.json"}]}}},
    })
    write(reply / "codemodel-v2-1.json", {
        "kind": "codemodel", "version": {"major": 2, "minor": 6},
        "paths": {"source": str(root).replace("\\", "/"), "build": str(build).replace("\\", "/")},
        "configurations": [{"name": "Debug", "targets": [
            {"name": "core", "id": "core::@1", "jsonFile": "target-core-Debug.json"}]}],
    })
    write(reply / "target-core-Debug.json", {
        "name": "core", "id": "core::@1", "type": "STATIC_LIBRARY",
        "sources": [{"path": "src/a.cpp", "compileGroupIndex": 0}],
        "compileGroups": [{"language": "CXX", "sourceIndexes": [0]}],
    })
    write(build / "x.sln", 'Project("{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}") = "core", "core.vcxproj", '
                           '"{11111111-1111-1111-1111-111111111111}"\n')
    write(build / "core.vcxproj", CORE)
    return root, build


def test_vs_generator_compile_one_uses_the_generated_project(tmp_path, allowed):
    root, build = vs_generator_tree(allowed)
    p = planner(tmp_path, host="windows", tools={"msbuild": MSBUILD, "cmake": "C:\\cmake\\cmake.exe"})
    request = BuildJobRequest(project=str(root), build_dir="out/vs", action="compile_one", file="src/a.cpp")
    model = p.plan_model(request.model_request(), ctx())
    assert model.generator.value == "Visual Studio 17 2022"
    result = p.plan_run(request, model, ctx())
    assert result.template_id == "msbuild.compile_one"
    assert result.argv[1] == str(build / "core.vcxproj")
    trace = BuildJobRequest(project=str(root), build_dir="out/vs", action="include_trace", file="src/a.cpp")
    with pytest.raises(SonderError) as excinfo:
        p.plan_run(trace, model, ctx())
    assert excinfo.value.code == "RUNNER_UNAVAILABLE"


def cmake_tree(allowed, *, cache=""):
    root = allowed / "cm"
    write(root / "CMakeLists.txt", "project(x)\n")
    write(root / "src/a.cpp", "int a;\n")
    build = root / "build"
    write(build / "compile_commands.json", [
        {"directory": str(build), "file": str(root / "src/a.cpp"),
         "command": "/usr/bin/g++ -I%s -o CMakeFiles/a.o -c %s" % (root / "src", root / "src/a.cpp")}])
    write(build / "CMakeCache.txt", "CMAKE_GENERATOR:INTERNAL=Ninja\nCMAKE_BUILD_TYPE:STRING=Debug\n" + cache)
    write(build / "build.ninja", "")
    return root, build


def test_a_compiler_launcher_in_the_cache_reaches_the_network_decision(tmp_path, allowed):
    root, _ = cmake_tree(allowed, cache="CMAKE_CXX_COMPILER_LAUNCHER:STRING=/usr/bin/sccache\n")
    network = NetworkIsolation(mode="default", platform="linux", lookup=Lookup({"unshare": "/usr/bin/unshare"}),
                               run=lambda *a, **k: SimpleNamespace(outcome="ok", output=""),
                               geteuid=lambda: 1000, executable_guard=lambda path: path)
    result, _ = plan(planner(tmp_path, network=network), root, build_dir="build")
    assert result.network == "advisory_off" and result.argv[0] == "/usr/bin/cmake"
    assert any("sccache" in note and "daemon" in note for note in result.notes)
    strict = NetworkIsolation(mode="enforce", platform="linux", lookup=Lookup({"unshare": "/usr/bin/unshare"}),
                              run=lambda *a, **k: SimpleNamespace(outcome="ok", output=""),
                              geteuid=lambda: 1000, executable_guard=lambda path: path)
    with pytest.raises(SonderError) as excinfo:
        plan(planner(tmp_path, network=strict), root, build_dir="build")
    assert excinfo.value.code == "NETWORK_ISOLATION_UNAVAILABLE"


def test_enforced_network_prefixes_unshare_and_the_digest_is_stable(tmp_path, allowed):
    root, _ = cmake_tree(allowed)
    p = planner(tmp_path)
    first, _ = plan(p, root, build_dir="build")
    second, _ = plan(p, root, build_dir="build")
    assert first.argv[:3] == ("/usr/bin/unshare", "-rn", "--") and first.network == "enforced_off"
    assert "/usr/bin/unshare" in first.checked_executables
    assert first.command_digest == second.command_digest and first.log_dir != second.log_dir
    allowed_net, _ = plan(p, root, build_dir="build", allow_network=True)
    assert allowed_net.command_digest != first.command_digest and allowed_net.network == "allowed"
    wire = first.resolved_command()
    assert set(wire) >= {"template_id", "command_digest", "world", "network", "target", "config", "platform"}
    assert str(allowed) not in json.dumps(wire)


def test_bare_ninja_compile_one_and_trace_from_the_compile_db(tmp_path, allowed):
    root, build = cmake_tree(allowed)
    p = planner(tmp_path)
    result, model = plan(p, root, build_dir="build", action="compile_one", file="src/a.cpp")
    assert model.source.value == "compile_db"
    trace, _ = plan(p, root, build_dir="build", action="include_trace", file="src/a.cpp")
    assert trace.argv[3] == "/usr/bin/g++" and "-H" in trace.argv and "-o" not in trace.argv
    assert trace.cwd == str(build)


def test_roots_and_build_dir_refusals(tmp_path, allowed):
    root, _ = cmake_tree(allowed)
    p = planner(tmp_path)
    for kwargs, code in (
        ({"build_dir": "."}, "BUILD_TREE_REJECTED"),
        ({"build_dir": ".."}, "BUILD_TREE_REJECTED"),
    ):
        with pytest.raises(SonderError) as excinfo:
            p.locate(BuildModelRequest(project=str(root), **kwargs), ctx())
        assert excinfo.value.code == code
    with pytest.raises(SonderError) as excinfo:
        p.locate(BuildModelRequest(project=str(tmp_path)), ctx())
    assert excinfo.value.code == "PROJECT_OUTSIDE_ROOTS"
    with pytest.raises(SonderError) as excinfo:
        p.locate(BuildModelRequest(project=str(root), build_dir=str(tmp_path / "elsewhere")), ctx())
    assert excinfo.value.code == "PROJECT_OUTSIDE_ROOTS"
    if os.name != "nt":
        (root / "linked").symlink_to(root / "build")
        with pytest.raises(SonderError) as excinfo:
            p.locate(BuildModelRequest(project=str(root), build_dir="linked"), ctx())
        assert excinfo.value.code == "BUILD_TREE_REJECTED"


def test_a_missing_tree_says_configure_first(tmp_path, allowed):
    root = allowed / "fresh"
    write(root / "CMakeLists.txt", "project(x)\n")
    p = planner(tmp_path)
    with pytest.raises(SonderError) as excinfo:
        p.plan_model(BuildModelRequest(project=str(root)), ctx())
    assert excinfo.value.code == "BUILD_TREE_MISSING" and "configure" in str(excinfo.value)
    configure = BuildJobRequest(project=str(root), action="configure", generator="Ninja", config="Debug")
    result = p.plan_run(configure, None, ctx())
    assert result.template_id == "cmake.configure" and "-DCMAKE_BUILD_TYPE=Debug" in result.argv
    assert "-DFETCHCONTENT_FULLY_DISCONNECTED=ON" in result.argv
    assert result.pre_writes and result.pre_writes[0][0].endswith("query.json")
    assert result.build_dir == str(root / "build")


def test_configure_presets_resolve_the_binary_dir(tmp_path, allowed):
    root = allowed / "presets"
    write(root / "CMakeLists.txt", "project(x)\n")
    write(root / "CMakePresets.json", {"version": 6, "include": ["presets/common.json"],
                                       "configurePresets": [
                                           {"name": "dbg", "inherits": "base", "generator": "Ninja",
                                            "binaryDir": "${sourceDir}/build/dbg"},
                                           {"name": "envdir", "generator": "Ninja",
                                            "binaryDir": "$env{HOME}/b"}]})
    write(root / "presets/common.json", {"version": 6, "configurePresets": [{"name": "base", "hidden": True}]})
    p = planner(tmp_path)
    result = p.plan_run(BuildJobRequest(project=str(root), action="configure", preset="dbg"), None, ctx())
    assert result.template_id == "cmake.configure.preset" and result.build_dir == str(root / "build" / "dbg")
    assert result.cwd == str(root)
    for name in ("envdir", "base", "missing"):
        with pytest.raises(SonderError) as excinfo:
            p.plan_run(BuildJobRequest(project=str(root), action="configure", preset=name), None, ctx())
        assert excinfo.value.code == "UNKNOWN_PRESET", name


def test_windows_ninja_with_msvc_needs_vcvars(tmp_path, allowed):
    root = allowed / "win"
    write(root / "CMakeLists.txt", "project(x)\n")
    env = Env()
    p = planner(tmp_path, host="windows", env=env,
                tools={"cmake": "C:\\cmake\\cmake.exe", "ninja": "C:\\ninja\\ninja.exe"})
    p.plan_run(BuildJobRequest(project=str(root), action="configure", generator="Ninja"), None, ctx())
    assert env.calls[-1] == ("cmake", "msvc")
    with pytest.raises(SonderError) as excinfo:  # VS generators never capture vcvars
        planner(tmp_path).plan_run(BuildJobRequest(project=str(root), action="configure",
                                                   generator="Visual Studio 17 2022"), None, ctx())
    assert excinfo.value.code == "RUNNER_UNAVAILABLE"
    p.plan_run(BuildJobRequest(project=str(root), action="configure",
                               generator="Visual Studio 17 2022"), None, ctx())
    assert env.calls[-1] == ("cmake", "")
