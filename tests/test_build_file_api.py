"""domain/build/file_api.py over real (sanitized) CMake 3.28 File API replies."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sonder_runtime.domain.build import file_api
from sonder_runtime.domain.build.model import (
    BuildDomainError,
    Generator,
    ModelSource,
    PchMode,
    TargetType,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cpp_build" / "file_api"
SOURCE = "/work/sparklite"


def _files(name: str) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in (FIXTURES / name).iterdir()}


def _model(name: str, build_sub: str, files: dict[str, bytes] | None = None, **kwargs):
    files = files if files is not None else _files(name)
    index = file_api.parse_reply_index(files[file_api.newest_index_name(files)])
    return file_api.model_from_file_api(
        index=index, objects=files, source_root=SOURCE,
        build_dir=SOURCE + "/build/" + build_sub, project_label="sparklite", **kwargs)


def test_real_reply_parses_into_a_model():
    model = _model("sonder", "ninja-debug")
    assert model.source is ModelSource.FILE_API and model.file_api_available
    assert model.generator is Generator.NINJA and not model.multi_config
    assert model.configs == ("Debug",)
    assert model.cmake_version == "3.28.3"
    assert model.reply_client == "client-sonder"
    names = {target.name: target for target in model.targets}
    assert set(names) == {"core", "game", "crash_test", "shadergen", "shaders", "deploy", "slow"}
    assert names["core"].type is TargetType.STATIC_LIBRARY
    assert names["game"].depends == ("core", "shaders")
    assert names["deploy"].utility and names["slow"].utility and names["shaders"].utility
    assert names["shadergen"].build_time_tool and not names["core"].build_time_tool
    assert model.toolchains[0].family == "gnu" and model.toolchains[0].path_label == "<external>/c++"
    assert "CMakeLists.txt" in model.build_inputs
    assert not model.truncated


def test_precompile_headers_map_to_the_real_header():
    model = _model("sonder", "ninja-debug")
    core = model.target("core")
    assert core.pch_headers == ("src/core/pch.h",)
    math = model.units_for("src/core/math.cpp")[0]
    assert math.pch is PchMode.USE and math.pch_header == "src/core/pch.h"
    assert all("cmake_pch" not in unit.file_label for unit in model.units)
    main = model.units_for("src/game/main.cpp")[0]
    assert main.pch is PchMode.NONE and main.include_dir_count == 2 and main.std == "17"


def test_a_second_clients_reply_is_read_without_a_sonder_query():
    files = _files("vscode")
    index = file_api.parse_reply_index(files[file_api.newest_index_name(files)])
    assert index.clients == ("client-vscode",)
    model = _model("vscode", "vscode", files)
    assert model.reply_client == "client-vscode"
    assert {unit.file_rel for unit in model.units} >= {"src/core/math.cpp", "src/game/main.cpp"}
    assert model.toolchains == ()
    assert any("toolchains-v1 reply not present" in note for note in model.notes)


def test_toolchains_are_absent_before_cmake_320():
    files = _files("vscode")
    name = file_api.newest_index_name(files)
    document = json.loads(files[name])
    document["cmake"]["version"]["string"] = "3.19.8"
    files[name] = json.dumps(document).encode()
    model = _model("vscode", "vscode", files)
    assert any("needs CMake >= 3.20" in note for note in model.notes)


def test_unity_blob_mapping():
    model = _model("unity", "ninja-unity")
    core = model.target("core")
    assert core.unity
    math = model.units_for("src/core/math.cpp")[0]
    assert math.unity_blob_rel == "CMakeFiles/core.dir/Unity/unity_0_cxx.cxx"
    assert math.pch_header == "src/core/pch.h" and math.std == "17"


def test_multiple_unity_blobs_are_mapped_from_their_text_or_left_unmapped():
    files = _files("unity")
    core_name = next(name for name in files if name.startswith("target-core-"))
    document = json.loads(files[core_name])
    blob = dict(document["sources"][1])
    blob["path"] = "build/ninja-unity/CMakeFiles/core.dir/Unity/unity_1_cxx.cxx"
    document["sources"].append(blob)
    document["compileGroups"][1]["sourceIndexes"].append(len(document["sources"]) - 1)
    files[core_name] = json.dumps(document).encode()
    unmapped = _model("unity", "ninja-unity", files)
    assert unmapped.units_for("src/core/math.cpp")[0].unity_blob_rel == ""
    assert any("unity blob membership unknown" in note for note in unmapped.notes)
    texts = {
        "CMakeFiles/core.dir/Unity/unity_0_cxx.cxx": b'#include "/work/sparklite/src/core/math.cpp"\n',
        "CMakeFiles/core.dir/Unity/unity_1_cxx.cxx": b'#include "/work/sparklite/src/core/entity.cpp"\n',
    }
    mapped = _model("unity", "ninja-unity", files, unity_blobs=texts)
    assert mapped.units_for("src/core/entity.cpp")[0].unity_blob_rel.endswith("unity_1_cxx.cxx")
    assert mapped.units_for("src/core/math.cpp")[0].unity_blob_rel.endswith("unity_0_cxx.cxx")


def test_an_index_naming_a_missing_file_sets_truncated():
    files = _files("sonder")
    toolchains = next(name for name in files if name.startswith("toolchains-v1-"))
    del files[toolchains]
    model = _model("sonder", "ninja-debug", files)
    assert model.truncated
    assert any(toolchains in note for note in model.notes)
    files = _files("sonder")
    game = next(name for name in files if name.startswith("target-game-"))
    del files[game]
    model = _model("sonder", "ninja-debug", files)
    assert model.truncated and model.target("game") is None


def test_oversize_deep_and_malformed_input_is_refused():
    files = _files("sonder")
    codemodel = next(name for name in files if name.startswith("codemodel-v2-"))
    oversize = dict(files)
    oversize[codemodel] = b" " * (file_api.MAX_OBJECT_BYTES + 1)
    with pytest.raises(BuildDomainError) as excinfo:
        _model("sonder", "ninja-debug", oversize)
    assert excinfo.value.code == "BUILD_TREE_REJECTED"
    deep = dict(files)
    deep[codemodel] = ("[" * 80 + "]" * 80).encode()
    with pytest.raises(BuildDomainError):
        _model("sonder", "ninja-debug", deep)
    garbage = dict(files)
    garbage[codemodel] = b"{not json"
    with pytest.raises(BuildDomainError):
        _model("sonder", "ninja-debug", garbage)
    with pytest.raises(BuildDomainError):
        file_api.parse_reply_index(b"x" * (file_api.MAX_INDEX_BYTES + 1))
    too_many = {"f%d.json" % i: b"{}" for i in range(file_api.MAX_REPLY_FILES + 1)}
    index = file_api.parse_reply_index(files[file_api.newest_index_name(files)])
    with pytest.raises(BuildDomainError):
        file_api.model_from_file_api(index=index, objects=too_many, source_root=SOURCE,
                                     build_dir=SOURCE + "/build", project_label="x")


def test_a_tree_configured_for_another_source_is_refused():
    files = _files("sonder")
    index = file_api.parse_reply_index(files[file_api.newest_index_name(files)])
    with pytest.raises(BuildDomainError) as excinfo:
        file_api.model_from_file_api(index=index, objects=files, source_root="/elsewhere/proj",
                                     build_dir="/elsewhere/proj/build", project_label="x")
    assert excinfo.value.code == "BUILD_TREE_REJECTED"


def test_index_names_query_and_cache_helpers():
    assert file_api.newest_index_name([
        "index-2026-01-01T00-00-00-0001.json", "index-2026-09-25T11-39-56-0619.json",
        "codemodel-v2-x.json", "../index-9999.json",
    ]) == "index-2026-09-25T11-39-56-0619.json"
    assert file_api.newest_index_name(["cache-v2.json"]) is None
    query = json.loads(file_api.query_document())
    assert {item["kind"] for item in query["requests"]} == {"codemodel", "cache", "toolchains",
                                                            "cmakeFiles"}
    cache = file_api.parse_cmake_cache_text(
        b"# comment\nCMAKE_CXX_COMPILER_LAUNCHER:STRING=/usr/bin/sccache\n"
        b"SPARK_PRIVATE_OPTION:STRING=on\nCMAKE_BUILD_TYPE:STRING=Debug\n")
    assert cache == {"CMAKE_CXX_COMPILER_LAUNCHER": "/usr/bin/sccache", "CMAKE_BUILD_TYPE": "Debug"}
    assert file_api.cache_launchers(cache) == ("sccache",)
    files = _files("sonder")
    cache_v2 = next(name for name in files if name.startswith("cache-v2-"))
    parsed = file_api.parse_cache_v2(files[cache_v2])
    assert parsed["CMAKE_BUILD_TYPE"] == "Debug" and set(parsed) <= file_api.CACHE_ALLOWLIST


def test_codemodel_target_files_lists_only_reply_names():
    files = _files("sonder")
    codemodel = next(name for name in files if name.startswith("codemodel-v2-"))
    names = file_api.codemodel_target_files(files[codemodel])
    assert len(names) == 7 and all(name in files for name in names)
    info = file_api.parse_codemodel_v2(files[codemodel], files)
    assert info.source == SOURCE and info.configs == ("Debug",) and info.missing == ()
    assert "shadergen" in info.target_names
    partial = file_api.parse_codemodel_v2(files[codemodel], {})
    assert len(partial.missing) == 7
    hostile = json.dumps({"configurations": [{"targets": [
        {"jsonFile": "../../etc/passwd", "name": "x"}, {"jsonFile": "ok.json", "name": "y"}]}]})
    assert file_api.codemodel_target_files(hostile.encode()) == ("ok.json",)
