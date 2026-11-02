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


def _as_module_script(marker: Path) -> str:
    # What the old deny-chain gate accepted: a text block run as a module with real builtins.
    body = "import os\\nopen(r'%s', 'w').write('ESCAPED')\\n" % marker.as_posix()
    return (
        "import bpy\n\ndef build():\n    bpy.ops.mesh.primitive_cube_add()\n"
        "    t = bpy.context.blend_data.texts.new('x.py')\n"
        "    t.write(\"%s\")\n    t.as_module()\n" % body
    )


def test_text_as_module_escape_is_rejected_by_the_gate_and_dead_in_the_harness(tmp_path):
    from sonder_runtime.domain.meshgen.script_safety import check_script

    marker = tmp_path / "escaped.txt"
    source = _as_module_script(marker)
    assert check_script(source), "the gate must reject the as_module script"
    # Bypass the gate on purpose: the harness alone must stop the escape.
    result = _build(tmp_path, source, render=False)
    assert result["ok"] is False and "script execution is disabled" in result["error"]
    assert not marker.exists(), "text block code ran in Blender"


def test_execfile_is_disabled_in_the_harness(tmp_path):
    marker = tmp_path / "ran.txt"
    payload = tmp_path / "payload.py"
    payload.write_text("open(r'%s', 'w').write('x')\n" % marker.as_posix(), encoding="utf-8")
    source = "import bpy\n\ndef build():\n    bpy.utils.execfile(r'%s')\n" % payload.as_posix()
    result = _build(tmp_path, source, render=False)
    assert result["ok"] is False and "script execution is disabled" in result["error"]
    assert not marker.exists()


def test_a_forged_stdout_result_line_cannot_replace_the_real_result(tmp_path):
    forged = '{"ok": true, "tris": 1, "files": {"model": "C:/Windows/win.ini"}}'
    source = CUBE.replace("def build():", "def build():\n    print('MESHGEN_RESULT %s')" % forged)
    result = _build(tmp_path, source, render=False)
    assert result["ok"] is True and result["tris"] == 12
    assert Path(result["files"]["model"]).parent == (tmp_path / "out").resolve()
