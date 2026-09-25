"""domain/build/model.py: validation, labels, digest, views and the brief."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from sonder_runtime.domain.build import file_api
from sonder_runtime.domain.build.model import (
    BuildDomainError,
    BuildModel,
    BuildSystem,
    BuildTarget,
    CompileUnit,
    TargetType,
    build_context_summary,
    build_view,
    finalize_model,
    json_depth,
    loads_bounded_json,
    model_digest,
    model_to_wire,
    norm_path,
    path_label,
    rel_under,
    safe_rel,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cpp_build"
SOURCE = "/work/sparklite"
BUILD = "/work/sparklite/build/ninja-debug"


def _sparklite_model(created_at: float = 1.0) -> BuildModel:
    folder = FIXTURES / "file_api" / "sonder"
    files = {path.name: path.read_bytes() for path in folder.iterdir()}
    index = file_api.parse_reply_index(files[file_api.newest_index_name(files)])
    return file_api.model_from_file_api(index=index, objects=files, source_root=SOURCE,
                                        build_dir=BUILD, project_label="sparklite",
                                        created_at=created_at)


def test_target_names_are_validated():
    BuildTarget(name="core", type=TargetType.STATIC_LIBRARY)
    BuildTarget(name="Engine\\SparkLite_Core", type=TargetType.STATIC_LIBRARY)
    for bad in ("", "a b", "a;b", "-x", "a:b", "x\x00", "\\lead"):
        with pytest.raises(BuildDomainError):
            BuildTarget(name=bad, type=TargetType.EXECUTABLE)


def test_string_fields_refuse_control_characters_and_overflow():
    with pytest.raises(BuildDomainError):
        CompileUnit(file_label="a\nb", file_rel="a")
    with pytest.raises(BuildDomainError):
        CompileUnit(file_label="x" * 2000, file_rel="")
    with pytest.raises(BuildDomainError):
        BuildModel(project_label="", source_root="/s", build_dir="/b", system=BuildSystem.CMAKE)


def test_path_labels_never_expose_absolute_paths():
    assert path_label("/work/sparklite/src/a.cpp", source_root=SOURCE, build_dir=BUILD) == (
        "src/a.cpp", "src/a.cpp")
    assert path_label(BUILD + "/generated/x.h", source_root=SOURCE, build_dir=BUILD) == (
        "<build>/generated/x.h", "")
    assert path_label("/usr/include/c++/13/string", source_root=SOURCE, build_dir=BUILD) == (
        "<external>/string", "")
    label, rel = path_label("C:\\SRC\\Spark\\src\\m.cpp", source_root="c:/src/spark", build_dir="")
    assert (label, rel) == ("src/m.cpp", "src/m.cpp")


def test_rel_under_and_safe_rel():
    assert rel_under("/a/b/c", "/a/b") == "c"
    assert rel_under("/a/bc", "/a/b") is None
    assert rel_under("C:\\X\\y", "c:/x") == "y"
    assert rel_under("/a/b", "/a/b") == "."
    assert safe_rel("../x") is None and safe_rel("/abs") is None and safe_rel("C:/x") is None
    assert safe_rel("a/./b/../c") == "a/c"
    assert norm_path("\\\\server\\share\\x") == "//server/share/x"


def test_bounded_json_refuses_depth_size_and_garbage():
    assert json_depth('{"a": "[[[[", "b": [[1]]}') == 3
    with pytest.raises(BuildDomainError) as excinfo:
        loads_bounded_json("[" * 100 + "]" * 100, max_bytes=10_000, what="x")
    assert excinfo.value.code == "BUILD_TREE_REJECTED"
    with pytest.raises(BuildDomainError):
        loads_bounded_json(b"{}" * 10, max_bytes=4, what="x")
    with pytest.raises(BuildDomainError):
        loads_bounded_json(b"{nope", max_bytes=100, what="x")


def test_digest_ignores_created_at_and_tracks_content():
    first = _sparklite_model(created_at=1.0)
    second = _sparklite_model(created_at=99.0)
    assert first.digest == second.digest == model_digest(first)
    changed = finalize_model(replace(first, configs=("Debug", "Release")))
    assert changed.digest != first.digest


def test_wire_forms_are_label_only():
    model = _sparklite_model()
    text = json.dumps(model_to_wire(model))
    assert "/work/" not in text
    for detail in ("targets", "compile_units", "toolchain", "presets"):
        wire = model_to_wire(build_view(model, detail=detail, max_items=500))
        assert "/work/" not in json.dumps(wire)
        assert wire["detail"] == detail


def test_view_bounds_and_target_filter():
    model = _sparklite_model()
    view = build_view(model, detail="compile_units", max_items=2)
    assert len(view.items) == 2 and view.truncated and view.total == len(model.units)
    only = build_view(model, detail="compile_units", target="core")
    assert {unit.target for unit in only.items} == {"core"}
    with pytest.raises(BuildDomainError) as excinfo:
        build_view(model, detail="targets", target="nope")
    assert excinfo.value.code == "UNKNOWN_TARGET"
    with pytest.raises(BuildDomainError):
        build_view(model, detail="everything")


def test_context_summary_is_bounded_and_flags_tools():
    model = _sparklite_model()
    text = build_context_summary(model, max_chars=600)
    assert len(text) <= 600
    assert "shadergen" in text and "utility(refused)" in text and "/work" not in text
    assert len(build_context_summary(model, max_chars=100)) <= 100
