"""domain/build/msbuild.py: solution/vcxproj/props without evaluation."""
from __future__ import annotations

from pathlib import Path

import pytest

from sonder_runtime.domain.build.model import (
    BuildDomainError,
    BuildSystem,
    Generator,
    ModelSource,
    PchMode,
    TargetType,
)
from sonder_runtime.domain.build.msbuild import (
    MAX_XML_ELEMENTS,
    cleanse_project_name,
    condition_key,
    merge_props,
    model_from_msbuild,
    parse_solution,
    parse_vcxproj,
    resolve_import_path,
    safe_xml_root,
    solution_target_name,
)
from sonder_runtime.domain.build.tool_targets import classify_targets

ROOT = Path(__file__).parent / "fixtures" / "cpp_build" / "msbuild"


def _projects() -> dict[str, bytes]:
    return {rel: (ROOT / rel).read_bytes() for rel in (
        "src/core/core.vcxproj", "src/game/game.vcxproj", "tools/tools.vcxproj")}


def _props() -> dict[str, bytes]:
    return {rel: (ROOT / rel).read_bytes() for rel in ("props/engine.props", "props/common.props")}


def _model():
    return model_from_msbuild(solution=(ROOT / "SparkLite.sln").read_bytes(),
                              solution_label="SparkLite.sln", projects=_projects(), props=_props(),
                              source_root="C:/src/SparkLite", project_label="SparkLite")


def test_solution_configs_platforms_folders_and_mangling():
    info = parse_solution((ROOT / "SparkLite.sln").read_bytes())
    assert info.vs_major == 17
    assert info.configs == ("Debug", "Release")
    assert info.platforms == ("x64", "Gaming.Xbox.Scarlett.x64")
    names = {item.name: item for item in info.buildable()}
    assert names["SparkLite.Core"].target_name == "Engine\\SparkLite_Core"
    assert names["SparkLite.Game"].folders == ("Engine",)
    assert names["Tools"].target_name == "Tools"
    assert names["SparkLite.Core"].path == "src/core/core.vcxproj"
    assert len(info.projects) == 4 and len(info.buildable()) == 3


def test_mangling_rules():
    assert cleanse_project_name("My.Project(1)'s%$@;") == "My_Project_1__s____"
    assert solution_target_name("A.B", ("Engine", "Sub.Dir")) == "Engine\\Sub_Dir\\A_B"


def test_model_matrix_custom_platform_and_toolset():
    model = _model()
    assert model.system is BuildSystem.MSBUILD and model.source is ModelSource.MSBUILD
    assert model.generator is Generator.VS2022 and model.multi_config
    assert "Gaming.Xbox.Scarlett.x64" in model.platforms
    core = model.target("Engine\\SparkLite_Core")
    assert core is not None and core.type is TargetType.STATIC_LIBRARY
    assert core.project == "src/core/core.vcxproj" and core.folder == "Engine"
    assert core.configs == ("Debug", "Release")
    game = model.target("Engine\\SparkLite_Game")
    assert game.type is TargetType.EXECUTABLE


def test_pch_and_standard_come_from_imported_props():
    model = _model()
    math = model.units_for("src/core/math.cpp")[0]
    assert math.pch is PchMode.USE and math.pch_header == "src/core/pch.h"
    assert math.std == "c++17"  # from props/common.props via $(MSBuildThisFileDirectory)
    assert "src/core/pch.h" in math.forced_includes
    assert math.define_count == 2 and math.family == "msvc"
    pch_cpp = model.units_for("src/core/pch.cpp")[0]
    assert pch_cpp.pch is PchMode.CREATE
    main = model.units_for("src/game/main.cpp")[0]
    assert main.std == "c++20" and main.pch is PchMode.NONE
    assert model.target("Engine\\SparkLite_Core").pch_headers == ("src/core/pch.h",)
    assert any("property sheet imports" in note for note in model.notes)


def test_makefile_projects_are_flagged_utility():
    model = _model()
    tools = model.target("Tools")
    assert tools.type is TargetType.MSBUILD_MAKEFILE and tools.utility
    assert "Tools" in classify_targets(model).utility


def test_props_merge_order_and_unresolvable_imports():
    vcx = parse_vcxproj(_projects()["src/core/core.vcxproj"], label="src/core/core.vcxproj")
    assert vcx.configurations[0] == ("Debug", "x64")
    merged = merge_props(vcx, _props())
    resolved = merged.resolve("Debug", "x64")
    assert resolved["PrecompiledHeader"] == "Use" and resolved["ConfigurationType"] == "StaticLibrary"
    console = merged.resolve("Debug", "Gaming.Xbox.Scarlett.x64")
    assert console["PlatformToolset"] == "v143"
    assert any("need MSBuild evaluation" in note for note in merged.notes)
    missing = merge_props(vcx, {})
    assert "PrecompiledHeader" not in missing.resolve("Debug", "x64")


def test_import_path_resolution_stays_in_root():
    assert resolve_import_path("src/core/core.vcxproj", "..\\..\\props\\engine.props") == "props/engine.props"
    assert resolve_import_path("props/engine.props", "$(MSBuildThisFileDirectory)common.props") == "props/common.props"
    assert resolve_import_path("src/core/core.vcxproj", "..\\..\\..\\outside.props") is None
    assert resolve_import_path("src/core/core.vcxproj", "$(SolutionDir)x.props") is None
    assert resolve_import_path("a.vcxproj", "C:\\abs.props") is None


def test_conditions():
    assert condition_key(None) == "*"
    assert condition_key("'$(Configuration)|$(Platform)'=='Debug|x64'") == "Debug|x64"
    assert condition_key(" '$(Configuration)'=='Release' ") == "Release|*"
    assert condition_key("exists('x')") is None


def test_doctype_entities_and_element_caps_are_refused():
    with pytest.raises(BuildDomainError):
        safe_xml_root(b'<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "x">]><Project>&a;</Project>')
    laughs = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
              b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
              b"<Project>&lol2;</Project>")
    with pytest.raises(BuildDomainError):
        safe_xml_root(laughs)
    with pytest.raises(BuildDomainError):
        safe_xml_root(b"<Project>" + b"<a/>" * MAX_XML_ELEMENTS + b"</Project>")
    safe_xml_root(b"<Project>" + b"<a/>" * 1000 + b"</Project>")
    with pytest.raises(BuildDomainError):
        safe_xml_root(b"<Project><unclosed></Project>")
    with pytest.raises(BuildDomainError):
        safe_xml_root(b"x" * (4 * 1024 * 1024 + 1))
    utf16 = '<?xml version="1.0" encoding="utf-16"?><Project/>'.encode("utf-16")
    assert safe_xml_root(utf16).tag == "Project"


def test_bare_vcxproj_without_solution():
    model = model_from_msbuild(solution=None, solution_label="", projects=_projects(),
                               props=_props(), source_root="C:/src/SparkLite", project_label="x")
    assert {target.name for target in model.targets} == {"core", "game", "tools"}
    assert "x64" in model.platforms


def test_missing_project_file_marks_truncated():
    projects = _projects()
    del projects["src/game/game.vcxproj"]
    model = model_from_msbuild(solution=(ROOT / "SparkLite.sln").read_bytes(),
                               solution_label="SparkLite.sln", projects=projects, props=_props(),
                               source_root="C:/src/SparkLite", project_label="x")
    assert model.truncated and model.target("Engine\\SparkLite_Game") is None
