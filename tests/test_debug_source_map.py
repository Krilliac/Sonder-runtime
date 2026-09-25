"""Build-machine source paths mapped to the local checkout by longest unique suffix."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from sonder_runtime.adapters.debugging.source_map import ProjectSourceMap

pytestmark = pytest.mark.unit


# Minimal report doubles: ``map_report`` only needs dataclasses with these fields.
@dataclass(frozen=True)
class Frame:
    index: int
    file: str = ""
    line: int | None = None
    local_file: str | None = None


@dataclass(frozen=True)
class Thread:
    thread_id: int
    frames: tuple = ()


@dataclass(frozen=True)
class Report:
    threads: tuple = ()
    notes: tuple = ()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "SparkEngine"
    for rel in ("Engine/Render/render.cpp", "Engine/Physics/physics.cpp",
                "Engine/Render/D3D12/device.cpp", "Tools/Render/render.cpp",
                "Game/main.cpp", ".git/objects/render.cpp", "venv/lib/render.cpp"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// %s\n" % rel)
    return root


def test_a_ci_path_maps_to_the_local_file_by_suffix(checkout):
    mapper = ProjectSourceMap([checkout])
    assert mapper.lookup(r"C:\agent\_work\3\s\Engine\Render\render.cpp") == ("Engine/Render/render.cpp", "")
    assert mapper.lookup("/build/agent/s/Engine/Physics/physics.cpp") == ("Engine/Physics/physics.cpp", "")
    assert mapper.lookup(r"C:\agent\_work\3\s\engine\render\d3d12\DEVICE.cpp")[0] == \
        "Engine/Render/D3D12/device.cpp"


def test_an_ambiguous_suffix_is_noted_not_guessed(checkout):
    mapper = ProjectSourceMap([checkout])
    local, note = mapper.lookup(r"D:\other\render.cpp")
    assert local is None and "matches 2 project files" in note


def test_posix_paths_are_case_sensitive(checkout):
    mapper = ProjectSourceMap([checkout])
    assert mapper.lookup("/src/game/MAIN.cpp") == (None, "")
    assert mapper.lookup("/src/game/main.cpp")[0] == "Game/main.cpp"


def test_nothing_outside_the_roots_or_in_skipped_dirs_is_named(checkout, tmp_path):
    outside = tmp_path / "secret" / "keys.cpp"
    outside.parent.mkdir()
    outside.write_text("x")
    mapper = ProjectSourceMap([checkout])
    assert mapper.lookup(str(outside)) == (None, "")
    assert mapper.lookup("/x/.git/objects/render.cpp")[0] is None or \
        ".git" not in mapper.lookup("/x/.git/objects/render.cpp")[0]
    (checkout / "Engine" / "link.cpp").symlink_to(outside)
    fresh = ProjectSourceMap([checkout], ttl_seconds=0)
    assert fresh.lookup("/ci/Engine/link.cpp") == (None, "")


def test_the_index_is_bounded_and_says_so(checkout):
    mapper = ProjectSourceMap([checkout], max_files=2)
    report = mapper.map_report(Report(threads=(Thread(1, (Frame(0, "/x/Game/main.cpp", 3),)),)))
    assert any("truncated" in note for note in report.notes)


def test_map_report_sets_local_file_and_keeps_the_recorded_path(checkout):
    mapper = ProjectSourceMap(lambda: [checkout])
    report = Report(threads=(Thread(1, (
        Frame(0, r"C:\agent\_work\3\s\Engine\Render\render.cpp", 42),
        Frame(1, r"C:\agent\_work\3\s\Engine\Render\render.cpp", 50),
        Frame(2, r"C:\agent\x\render.cpp", 7),
        Frame(3, "", None),
        Frame(4, "/ci/Game/main.cpp", 9, local_file="already.cpp"),
    )),))
    mapped = mapper.map_report(report)
    frames = mapped.threads[0].frames
    assert frames[0].local_file == "Engine/Render/render.cpp"
    assert frames[0].file == r"C:\agent\_work\3\s\Engine\Render\render.cpp"
    assert frames[1].local_file == "Engine/Render/render.cpp"
    assert frames[2].local_file is None and frames[3].local_file is None
    assert frames[4].local_file == "already.cpp"
    assert sum("not mapped" in note for note in mapped.notes) == 1


def test_missing_roots_map_nothing(tmp_path):
    mapper = ProjectSourceMap([tmp_path / "missing"])
    assert mapper.lookup("/x/a.cpp") == (None, "")
