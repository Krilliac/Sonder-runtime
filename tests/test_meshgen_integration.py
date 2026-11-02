"""Real headless-Blender build.  Skipped unless SONDER_BLENDER points at blender."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sonder_runtime.adapters.meshgen import blender_runner

BLENDER = os.environ.get("SONDER_BLENDER", "")

pytestmark = pytest.mark.skipif(not BLENDER or not os.path.isfile(BLENDER),
                                reason="SONDER_BLENDER is not set to a Blender executable")

CUBE = '''import bpy

def build():
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0.5))
    paint(bpy.context.active_object, "#cc3333")
'''


def _build(tmp_path, source, **kw):
    code = tmp_path / "cand.py"
    code.write_text(source, encoding="utf-8")
    return blender_runner.build_candidate(BLENDER, str(code), str(tmp_path / "out"), name="thing",
                                          max_tris=kw.pop("max_tris", 100), height_m=kw.pop("height_m", 0),
                                          timeout=240, **kw)


def test_a_cube_builds_exports_and_renders(tmp_path):
    result = _build(tmp_path, CUBE, height_m=2.0)
    assert result["ok"] is True, result
    assert result["tris"] == 12 and result["within_budget"] is True
    assert result["size"] == [2.0, 2.0, 2.0]
    for key in ("model", "sheet", "view"):
        path = Path(result["files"][key])
        assert path.is_file() and path.stat().st_size > 0, key
    assert Path(result["files"]["sheet"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert b"glTF" == Path(result["files"]["model"]).read_bytes()[:4]


def test_over_budget_is_reported_not_fatal(tmp_path):
    assert _build(tmp_path, CUBE, max_tris=6, render=False)["within_budget"] is False


def test_script_errors_come_back_as_a_result(tmp_path):
    result = _build(tmp_path, "import bpy\n\ndef build():\n    raise ValueError('wheel is flat')\n", render=False)
    assert result["ok"] is False and "ValueError: wheel is flat" in result["error"]


def test_restricted_builtins_block_open_and_imports(tmp_path):
    result = _build(tmp_path, "def build():\n    open('x.txt', 'w')\n", render=False)
    assert result["ok"] is False and "NameError" in result["error"]
    result = _build(tmp_path, "def build():\n    import os\n", render=False)
    assert result["ok"] is False and "not allowed" in result["error"]


def test_an_empty_scene_is_an_error(tmp_path):
    result = _build(tmp_path, "def build():\n    pass\n", render=False)
    assert result["ok"] is False and "no mesh objects" in result["error"]
