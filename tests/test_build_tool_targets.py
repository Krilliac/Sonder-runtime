"""domain/build/tool_targets.py: utility and build-time-tool classification (F4/F5)."""
from __future__ import annotations

from pathlib import Path

from sonder_runtime.domain.build import file_api
from sonder_runtime.domain.build.model import (
    BuildModel,
    BuildSystem,
    BuildTarget,
    CompileUnit,
    TargetType,
)
from sonder_runtime.domain.build.tool_targets import classify_targets

FIX = Path(__file__).parent / "fixtures" / "cpp_build" / "file_api" / "sonder"


def test_codegen_executable_consumed_by_a_custom_command_is_a_build_time_tool():
    files = {path.name: path.read_bytes() for path in FIX.iterdir()}
    index = file_api.parse_reply_index(files[file_api.newest_index_name(files)])
    model = file_api.model_from_file_api(index=index, objects=files, source_root="/work/sparklite",
                                         build_dir="/work/sparklite/build/ninja-debug",
                                         project_label="sparklite")
    safety = classify_targets(model)
    assert safety.build_time_tool == frozenset({"shadergen"})
    assert "tools/shadergen.cpp" in safety.tool_sources
    assert "CMakeLists.txt" in safety.tool_sources
    assert "src/core/math.cpp" not in safety.tool_sources
    assert safety.utility == frozenset({"deploy", "slow", "shaders"})
    assert not safety.is_refused_target("game")


def _unit(target: str, rel: str) -> CompileUnit:
    return CompileUnit(file_label=rel, file_rel=rel, target=target)


def test_libraries_linked_by_tools_are_tainted_transitively():
    model = BuildModel(
        project_label="engine", source_root="/e", build_dir="/e/b", system=BuildSystem.CMAKE,
        targets=(
            BuildTarget("base", TargetType.STATIC_LIBRARY),
            BuildTarget("reflect_core", TargetType.STATIC_LIBRARY, depends=("base",)),
            BuildTarget("reflector", TargetType.EXECUTABLE, depends=("reflect_core",)),
            BuildTarget("gen_headers", TargetType.UTILITY, depends=("reflector",)),
            BuildTarget("engine", TargetType.SHARED_LIBRARY, depends=("gen_headers", "base")),
            BuildTarget("baker", TargetType.EXECUTABLE, artifacts=("baker",)),
            BuildTarget("install", TargetType.UTILITY),
            BuildTarget("ALL_BUILD", TargetType.UTILITY),
        ),
        units=(_unit("base", "src/base.cpp"), _unit("reflect_core", "src/reflect.cpp"),
               _unit("reflector", "tools/reflector.cpp"), _unit("engine", "src/engine.cpp"),
               _unit("baker", "tools/baker.cpp")),
        build_inputs=("CMakeLists.txt", "cmake/bake.cmake", "bin/baker"),
    )
    safety = classify_targets(model)
    assert safety.build_time_tool == frozenset({"reflector", "baker"})
    assert {"src/base.cpp", "src/reflect.cpp", "tools/reflector.cpp", "tools/baker.cpp",
            "cmake/bake.cmake"} <= safety.tool_sources
    assert "src/engine.cpp" not in safety.tool_sources
    assert "install" in safety.utility and "ALL_BUILD" not in safety.utility
    assert any("libraries linked by build-time tools" in note for note in safety.notes)
