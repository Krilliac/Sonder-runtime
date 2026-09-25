"""domain/build/presets.py: includes, inherits and the limited macro set."""
from __future__ import annotations

import json
from pathlib import Path

from sonder_runtime.domain.build.presets import MAX_PRESET_FILES, parse_cmake_presets

SPARK = Path(__file__).parent / "fixtures" / "cpp_build" / "sparklite"
ROOT = "/work/sparklite"


def _doc(**body) -> bytes:
    return json.dumps({"version": 6, **body}).encode()


def test_sparklite_presets_follow_includes_and_hide_hidden():
    presets = parse_cmake_presets(
        (SPARK / "CMakePresets.json").read_bytes(),
        includes={"presets/common.json": (SPARK / "presets" / "common.json").read_bytes()},
        source_root=ROOT)
    assert [item.name for item in presets.configure] == ["ninja-debug", "make-debug", "ninja-unity"]
    assert presets.find("base") is None
    ninja = presets.find("ninja-debug")
    assert ninja.generator == "Ninja" and ninja.binary_dir_resolvable
    assert ninja.binary_dir == ROOT + "/build/ninja-debug"
    assert ninja.binary_dir_label == "build/ninja-debug"
    assert dict(ninja.cache)["CMAKE_EXPORT_COMPILE_COMMANDS"] == "ON"
    assert dict(presets.find("ninja-unity").cache)["CMAKE_UNITY_BUILD"] == "ON"
    build = presets.find("make-debug", "build")
    assert build.configure_preset == "make-debug" and build.binary_dir == ROOT + "/build/make-debug"
    assert presets.files == ("CMakePresets.json", "presets/common.json")
    assert not presets.truncated


def test_include_outside_root_and_missing_include():
    outside = parse_cmake_presets(_doc(include=["../evil.json", "/etc/presets.json"]),
                                  includes={}, source_root=ROOT)
    assert outside.truncated
    assert sum("outside the source root" in note for note in outside.notes) == 2
    missing = parse_cmake_presets(_doc(include=["presets/nope.json"]), includes={},
                                  source_root=ROOT)
    assert missing.truncated and any("not available" in note for note in missing.notes)


def test_include_depth_and_count_caps():
    includes = {"p%d.json" % i: _doc(include=["p%d.json" % (i + 1)]) for i in range(10)}
    deep = parse_cmake_presets(_doc(include=["p0.json"]), includes=includes, source_root=ROOT)
    assert deep.truncated and len(deep.files) == 5
    wide = {"w%d.json" % i: _doc() for i in range(40)}
    many = parse_cmake_presets(_doc(include=sorted(wide)), includes=wide, source_root=ROOT)
    assert len(many.files) == MAX_PRESET_FILES and many.truncated


def test_binary_dir_macros():
    presets = parse_cmake_presets(_doc(configurePresets=[
        {"name": "env", "binaryDir": "$env{BUILD_ROOT}/x"},
        {"name": "penv", "binaryDir": "$penv{HOME}/x"},
        {"name": "parent", "binaryDir": "${sourceParentDir}/out/${presetName}"},
        {"name": "unknown", "binaryDir": "${fileDir}/x"},
        {"name": "relative", "generator": "Ninja", "binaryDir": "out/${generator}"},
        {"name": "none"},
    ]), includes={}, source_root=ROOT)
    assert not presets.find("env").binary_dir_resolvable
    assert not presets.find("penv").binary_dir_resolvable
    assert not presets.find("unknown").binary_dir_resolvable
    assert not presets.find("none").binary_dir_resolvable
    assert presets.find("parent").binary_dir == "/work/out/parent"
    assert presets.find("parent").binary_dir_label.startswith("<external>")
    assert presets.find("relative").binary_dir == ROOT + "/out/Ninja"


def test_inherits_order_cycles_and_conditions():
    presets = parse_cmake_presets(_doc(configurePresets=[
        {"name": "a", "hidden": True, "generator": "Ninja", "binaryDir": "${sourceDir}/a",
         "cacheVariables": {"CMAKE_BUILD_TYPE": "Debug"}},
        {"name": "b", "hidden": True, "generator": "Unix Makefiles",
         "cacheVariables": {"CMAKE_BUILD_TYPE": "Release", "CMAKE_UNITY_BUILD": True}},
        {"name": "child", "inherits": ["a", "b"]},
        {"name": "loop1", "inherits": "loop2"},
        {"name": "loop2", "inherits": "loop1"},
        {"name": "win-only", "binaryDir": "${sourceDir}/w",
         "condition": {"type": "equals", "lhs": "${hostSystemName}", "rhs": "Windows"}},
    ]), includes={}, source_root=ROOT)
    child = presets.find("child")
    assert child.generator == "Ninja" and child.binary_dir == ROOT + "/a"
    assert dict(child.cache) == {"CMAKE_BUILD_TYPE": "Debug", "CMAKE_UNITY_BUILD": "ON"}
    assert presets.find("loop1") is not None and not presets.find("loop1").binary_dir_resolvable
    assert presets.find("win-only") is None
    windows = parse_cmake_presets(_doc(configurePresets=[
        {"name": "win-only", "binaryDir": "${sourceDir}/w",
         "condition": {"type": "equals", "lhs": "${hostSystemName}", "rhs": "Windows"}}]),
        includes={}, source_root="C:/src/x", host_system="Windows")
    assert windows.find("win-only").binary_dir == "C:/src/x/w"


def test_user_presets_and_bad_documents():
    user = parse_cmake_presets(_doc(configurePresets=[{"name": "shared", "binaryDir": "b"}]),
                               includes={}, source_root=ROOT,
                               user_data=_doc(configurePresets=[{"name": "mine", "binaryDir": "m"}]))
    assert {item.name for item in user.configure} == {"shared", "mine"}
    bad = parse_cmake_presets(b"[1, 2", includes={}, source_root=ROOT)
    assert bad.truncated and bad.configure == ()
    odd = parse_cmake_presets(_doc(configurePresets=[{"name": "has space"}, {"name": "-x"}]),
                              includes={}, source_root=ROOT)
    assert odd.configure == ()
