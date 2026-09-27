"""GuardedBuildTreeReader: any client's File API reply, bounded no-follow
reads, preset includes and .props imports inside the root only."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sonder_runtime.adapters.build import tree_reader as reader_module
from sonder_runtime.adapters.build.tree_reader import GuardedBuildTreeReader, label_path
from sonder_runtime.domain.common.errors import SonderError

pytestmark = pytest.mark.unit

needs_symlinks = pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (dict, list)):
        data = json.dumps(data)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def reply_tree(build: Path, client="client-vscode"):
    reply = build / ".cmake" / "api" / "v1" / "reply"
    write(reply / "codemodel-v2-abc.json", {
        "kind": "codemodel", "version": {"major": 2, "minor": 6},
        "configurations": [{"name": "Debug", "targets": [{"name": "core", "jsonFile": "target-core.json"}],
                            "directories": [{"jsonFile": "directory-.json"}]}]})
    write(reply / "target-core.json", {"name": "core"})
    write(reply / "directory-.json", {})
    write(reply / "cache-v2-1.json", {"entries": []})
    write(reply / "unlisted-secret.json", {"x": 1})
    write(reply / "index-2020.json", {"objects": []})
    write(reply / "index-2026-09-25T10-00-00-0000.json", {
        "cmake": {"version": {"string": "3.28.3"}},
        "objects": [{"kind": "cache", "version": {"major": 2}, "jsonFile": "cache-v2-1.json"}],
        "reply": {client: {"query.json": {"responses": [
            {"kind": "codemodel", "version": {"major": 2}, "jsonFile": "codemodel-v2-abc.json"}]}}},
    })
    return reply


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    write(root / "CMakeLists.txt", "project(x)\n")
    build = root / "build"
    reply_tree(build)
    write(build / "CMakeCache.txt", "# c\nCMAKE_GENERATOR:INTERNAL=Ninja\n"
          "CMAKE_CXX_COMPILER_LAUNCHER:STRING=/usr/bin/sccache\nSECRET_TOKEN:STRING=nope\n")
    write(build / "compile_commands.json", "[]")
    write(build / "build.ninja", "")
    return root, build


def test_any_clients_reply_is_read_without_a_sonder_query(project):
    root, build = project
    raw = GuardedBuildTreeReader().read(str(root), str(build))
    names = [name for name, _ in raw.reply_objects]
    assert raw.reply_index[0][0] == "index-2026-09-25T10-00-00-0000.json"
    assert set(names) == {"cache-v2-1.json", "codemodel-v2-abc.json", "target-core.json", "directory-.json"}
    assert "unlisted-secret.json" not in names
    assert raw.compile_db == b"[]" and raw.ninja_present and raw.cmake_lists_present
    assert dict(raw.cache_values) == {"CMAKE_GENERATOR": "Ninja",
                                      "CMAKE_CXX_COMPILER_LAUNCHER": "/usr/bin/sccache"}
    assert not raw.truncated and raw.fingerprint


def test_an_index_naming_a_missing_file_marks_truncated(project):
    root, build = project
    (build / ".cmake/api/v1/reply/target-core.json").unlink()
    raw = GuardedBuildTreeReader().read(str(root), str(build))
    assert raw.truncated and any("missing reply file" in note for note in raw.notes)


def test_the_fingerprint_changes_with_a_new_index(project):
    root, build = project
    reader = GuardedBuildTreeReader()
    before = reader.fingerprint(str(root), str(build))
    write(build / ".cmake/api/v1/reply/index-2027.json", {"objects": []})
    assert reader.fingerprint(str(root), str(build)) != before


@needs_symlinks
def test_a_symlinked_compile_db_is_refused(project, tmp_path):
    root, build = project
    target = write(tmp_path / "elsewhere.json", "[{\"file\": \"/etc/shadow\"}]")
    (build / "compile_commands.json").unlink()
    (build / "compile_commands.json").symlink_to(target)
    raw = GuardedBuildTreeReader().read(str(root), str(build))
    assert raw.compile_db is None
    assert any("compile_commands.json" in note and "refused" in note for note in raw.notes)


@needs_symlinks
def test_a_reply_outside_build_dir_is_refused(project, tmp_path):
    root, build = project
    outside = tmp_path / "outside-reply"
    reply_tree(outside)
    reply = build / ".cmake" / "api" / "v1" / "reply"
    for item in reply.iterdir():
        item.unlink()
    reply.rmdir()
    reply.symlink_to(outside / ".cmake" / "api" / "v1" / "reply")
    raw = GuardedBuildTreeReader().read(str(root), str(build))
    assert raw.reply_objects == () and raw.reply_index == ()
    assert any("symlink" in note for note in raw.notes)
    # and a single reply object that is a link is refused too
    reply.unlink()
    reply_tree(build)
    victim = build / ".cmake/api/v1/reply/target-core.json"
    victim.unlink()
    victim.symlink_to(outside / ".cmake/api/v1/reply/target-core.json")
    raw = GuardedBuildTreeReader().read(str(root), str(build))
    assert "target-core.json" not in dict(raw.reply_objects)


@needs_symlinks
def test_a_symlinked_build_dir_is_rejected(project, tmp_path):
    root, build = project
    link = root / "build-link"
    link.symlink_to(build)
    with pytest.raises(SonderError) as excinfo:
        GuardedBuildTreeReader().read(str(root), str(link))
    assert excinfo.value.code == "BUILD_TREE_REJECTED"


def test_the_project_root_or_its_ancestor_is_not_a_build_dir(project):
    root, _ = project
    for bad in (root, root.parent):
        with pytest.raises(SonderError) as excinfo:
            GuardedBuildTreeReader().read(str(root), str(bad))
        assert excinfo.value.code == "BUILD_TREE_REJECTED"


def test_oversize_input_truncates(project, monkeypatch):
    root, build = project
    monkeypatch.setattr(reader_module, "MAX_COMPILE_DB_BYTES", 1)
    monkeypatch.setattr(reader_module, "MAX_REPLY_OBJECT_BYTES", 40)
    raw = GuardedBuildTreeReader().read(str(root), str(build))
    assert raw.compile_db is None and raw.truncated
    assert any("exceeds" in note for note in raw.notes)


def test_too_deep_json_is_not_parsed(project):
    root, build = project
    reply = build / ".cmake/api/v1/reply"
    write(reply / "index-2030.json", "[" * 200 + "]" * 200)
    raw = GuardedBuildTreeReader().read(str(root), str(build))
    assert raw.reply_objects == () and raw.truncated


def test_preset_includes_are_followed_inside_the_root_only(tmp_path):
    root = tmp_path / "p"
    write(root / "CMakePresets.json", {"version": 6, "include": [
        "presets/common.json", "../escape.json", "/etc/passwd", "$env{HOME}/x.json"]})
    write(root / "presets/common.json", {"version": 6, "include": ["more/deeper.json"]})
    write(root / "presets/more/deeper.json", {"version": 6})
    write(tmp_path / "escape.json", {"version": 6})
    write(root / "CMakeUserPresets.json", {"version": 6})
    raw = GuardedBuildTreeReader().read_presets(str(root))
    assert [name for name, _ in raw.presets] == ["CMakePresets.json", "CMakeUserPresets.json"]
    assert [label for label, _ in raw.preset_includes] == ["presets/common.json",
                                                            "presets/more/deeper.json"]
    no_user = GuardedBuildTreeReader(user_presets=False).read_presets(str(root))
    assert [name for name, _ in no_user.presets] == ["CMakePresets.json"]


def test_preset_include_depth_is_capped(tmp_path, monkeypatch):
    root = tmp_path / "p"
    write(root / "CMakePresets.json", {"include": ["a0.json"]})
    for index in range(8):
        write(root / ("a%d.json" % index), {"include": ["a%d.json" % (index + 1)]})
    raw = GuardedBuildTreeReader().read_presets(str(root))
    assert len(raw.preset_includes) == 4 and raw.truncated


def test_solution_projects_and_props_imports(tmp_path):
    root = tmp_path / "eng"
    write(root / "SparkLite.sln",
          'Project("{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}") = "Core.Runtime", "src\\core\\core.vcxproj", "{11111111-1111-1111-1111-111111111111}"\n'
          'Project("{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}") = "evil", "..\\outside\\x.vcxproj", "{22222222-2222-2222-2222-222222222222}"\n')
    write(root / "src/core/core.vcxproj",
          '<Project><Import Project="..\\..\\props\\engine.props" />'
          '<Import Project="$(VCTargetsPath)\\Microsoft.Cpp.props" />'
          '<Import Project="C:\\abs\\x.props" /></Project>')
    write(root / "props/engine.props", '<Project><Import Project="pch.props" /></Project>')
    write(root / "props/pch.props", "<Project/>")
    raw = GuardedBuildTreeReader().read(str(root), "")
    assert raw.solution[0] == "SparkLite.sln"
    assert [label for label, _ in raw.vcxproj] == ["src/core/core.vcxproj"]
    assert [label for label, _ in raw.props_imports] == ["props/engine.props", "props/pch.props"]
    assert any("outside" in note for note in raw.notes)


def test_label_path_stays_inside(tmp_path):
    assert label_path("/p", "/p/b", "<build>/x/y.vcxproj") == os.path.join("/p/b", "x", "y.vcxproj")
    assert label_path("/p", "/p/b", "src/a.vcxproj") == os.path.join("/p", "src", "a.vcxproj")
    for bad in ("../x", "<build>/../../etc", "/etc/passwd"):
        with pytest.raises(SonderError):
            label_path("/p", "/p/b", bad)
