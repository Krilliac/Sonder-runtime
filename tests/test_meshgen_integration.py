"""Real headless-Blender builds of recorded scripts.  Skipped unless SONDER_BLENDER points at blender.

Model-written code never runs in Blender: the script is recorded on the host into an op list, and the
(sandboxed, on Windows) Blender replays that list with trusted code.  These tests record real
scripts and build them through the default runner, then feed the harness hand-crafted op lists to
show it refuses anything outside the grammar.
"""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from sonder_runtime.adapters.meshgen import blender_runner, sandbox_win
from sonder_runtime.domain.meshgen.dsl.errors import ScriptError
from sonder_runtime.domain.meshgen.dsl.manifest import build_manifest
from sonder_runtime.domain.meshgen.dsl.recorder import compile_script

BLENDER = os.environ.get("SONDER_BLENDER", "")
_ON_WINDOWS = os.name == "nt"
REAL_KIT = Path(r"D:\LanternfallGame\Lanternfall\tools\blender\lf_kit.py")

pytestmark = pytest.mark.skipif(not BLENDER or not os.path.isfile(BLENDER),
                                reason="SONDER_BLENDER is not set to a Blender executable")

CUBE = '''def build():
    c = primitive("cube", "Cube", size=1.0, loc=(0, 0, 0.5))
    paint(c, "#cc3333")
'''

TEST_KIT = '''"""A trusted test kit."""
import sys

def box(name="Box", size=1.0):
    import bpy
    bpy.ops.mesh.primitive_cube_add(size=size)
    obj = bpy.context.active_object
    obj.name = name
    return obj

def noisy():
    print('MESHGEN_RESULT {"ok": true, "tris": 1, "files": {"model": "C:/Windows/win.ini"}}')
    return box("Noisy")

def finalize_and_export(path):
    raise RuntimeError("must never be reachable")
'''


def _kit(tmp_path):
    folder = tmp_path / "kit"
    folder.mkdir(exist_ok=True)
    path = folder / "testkit.py"
    path.write_text(TEST_KIT, encoding="utf-8")
    return path


def _record(source, kit=None, only=None):
    """Record ``source`` with the kit's manifest; ``only`` limits the exposed kit names."""
    manifest = None
    if kit is not None:
        manifest = build_manifest(Path(kit).read_text(encoding="utf-8"), Path(kit).stem)
    recording = compile_script(source, manifest)
    names = sorted(manifest.exposed if only is None else only) if manifest else []
    return recording, names


def _run(tmp_path, ops_text, kit=None, allowed=(), **kw):
    ops = tmp_path / "ops.json"
    ops.write_text(ops_text, encoding="utf-8")
    return blender_runner.build_candidate(
        BLENDER, str(ops), str(tmp_path / "out"), name="thing", kit_path=str(kit) if kit else None,
        kit_allowed=allowed, max_tris=kw.pop("max_tris", 100), height_m=kw.pop("height_m", 0), timeout=240, **kw)


def _build(tmp_path, source, kit=None, **kw):
    recording, allowed = _record(source, kit)
    return _run(tmp_path, recording.text, kit, allowed, **kw)


def _doc(*ops):
    full = [{"id": i, "line": i, "op": name, "args": args, "kwargs": kwargs}
            for i, (name, args, kwargs) in enumerate(ops, start=1)]
    return json.dumps({"version": 1, "kit": "", "seed": 0, "ops": full, "log": []})


def _no_outputs(tmp_path):
    out = tmp_path / "out"
    return not out.exists() or not [p for p in out.rglob("*") if p.suffix in (".glb", ".fbx", ".png")]


def glb_colours(path):
    """The distinct COLOR_0 values of a GLB file, rounded."""
    data = Path(path).read_bytes()
    json_length, _ = struct.unpack_from("<II", data, 12)
    document = json.loads(data[20:20 + json_length])
    offset = 20 + json_length
    blob_length, _ = struct.unpack_from("<II", data, offset)
    blob = data[offset + 8:offset + 8 + blob_length]
    found = set()
    for mesh in document.get("meshes", []):
        for primitive in mesh["primitives"]:
            index = primitive["attributes"].get("COLOR_0")
            if index is None:
                continue
            accessor = document["accessors"][index]
            view = document["bufferViews"][accessor["bufferView"]]
            code, size, scale = {5126: ("f", 4, 1.0), 5121: ("B", 1, 255.0), 5123: ("H", 2, 65535.0)}[
                accessor["componentType"]]
            width = {"VEC3": 3, "VEC4": 4}[accessor["type"]]
            stride = view.get("byteStride") or size * width
            base = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
            for item in range(accessor["count"]):
                values = struct.unpack_from("<" + code * width, blob, base + item * stride)
                found.add(tuple(round(value / scale, 1) for value in values[:3]))
    return found


# ---------------------------------------------------------------- core primitives, no kit
def test_a_core_cube_builds_exports_and_renders_without_a_kit(tmp_path):
    result = _build(tmp_path, CUBE, height_m=2.0)
    assert result["ok"] is True, result
    assert result["tris"] == 12 and result["within_budget"] is True
    assert result["size"] == [2.0, 2.0, 2.0]
    for key in ("model", "sheet", "view"):
        path = Path(result["files"][key])
        assert path.is_file() and path.stat().st_size > 0, key
    assert Path(result["files"]["sheet"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert Path(result["files"]["model"]).read_bytes()[:4] == b"glTF"
    assert result["sandbox"] == ("appcontainer" if _ON_WINDOWS else "unsandboxed")
    assert not list((tmp_path / "out").glob(".run-*"))


def test_the_core_primitives_and_mesh_and_join_work(tmp_path):
    source = '''def build():
    a = primitive("cube", "A", size=1.0)
    b = primitive("uv_sphere", "B", radius=0.5, segments=8, rings=4, loc=(2, 0, 0))
    c = primitive("cylinder", "C", radius=0.3, depth=1.0, segments=8, loc=(4, 0, 0), rot=(0, 90, 0))
    d = primitive("cone", "D", radius=0.3, depth=1.0, segments=8, loc=(6, 0, 0))
    e = primitive("torus", "E", major_radius=0.5, minor_radius=0.1, segments=8, loc=(8, 0, 0))
    f = primitive("ico_sphere", "F", radius=0.4, subdivisions=1, loc=(10, 0, 0))
    g = mesh("G", [(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)], loc=(12, 0, 0))
    transform(a, loc=(0, 0, 1), rot=(0, 0, 45), scale=(1, 2, 1))
    join([a, b, c, d, e, f, g], "All")
'''
    result = _build(tmp_path, source, max_tris=5000, render=False)
    assert result["ok"] is True, result
    assert result["tris"] > 100


def test_over_budget_is_reported_not_fatal(tmp_path):
    assert _build(tmp_path, CUBE, max_tris=6, render=False)["within_budget"] is False


def test_modifiers_are_baked_and_triangles_counted_after_them(tmp_path):
    source = '''def build():
    box = primitive("cube", "Box", size=1.0, loc=(0, 0, 0.5))
    modifier(box, "BEVEL", width=0.05, segments=2)
    modifier(box, "SUBSURF", levels=1)
    shade_smooth(box)
    paint(box, "#8899aa")
'''
    result = _build(tmp_path, source, max_tris=5000, render=True)
    assert result["ok"] is True, result
    assert result["tris"] > 100, result["tris"]  # a bare cube is 12
    assert result["parts"] and result["parts"][0]["tris"] == result["tris"]
    assert Path(result["files"]["sheet"]).is_file()


def test_paint_faces_predicate_gives_two_distinct_vertex_colours_in_the_glb(tmp_path):
    source = '''def build():
    box = primitive("cube", "Box", size=1.0, loc=(0, 0, 0.5))
    paint(box, "#2244ff")
    paint_faces(box, lambda p: p.center.z > 0.4, "#ff2222")
'''
    result = _build(tmp_path, source, max_tris=100, render=False)
    assert result["ok"] is True, result
    colours = glb_colours(result["files"]["model"])
    assert len(colours) >= 2, colours
    assert any(r > 0.8 for r, g, b in colours) and any(b > 0.8 for r, g, b in colours), colours


def test_an_empty_scene_is_an_error(tmp_path):
    source = "def build():\n    a = primitive('cube')\n    delete(a)\n"
    result = _build(tmp_path, source, render=False)
    assert result["ok"] is False and "no mesh objects" in result["error"]


def test_replay_caps_are_enforced_even_for_hand_crafted_ops(tmp_path):
    big = _doc(("core.primitive", ["uv_sphere"], {"segments": 100000, "rings": 100000}))
    result = _run(tmp_path, big, render=False)
    assert result["ok"] is False and "line 1" in result["error"], result
    unknown_prop = _doc(("core.primitive", ["cube"], {}), ("core.modifier", [{"$ref": 1}, "BEVEL"], {"filepath": "x"}))
    result = _run(tmp_path, unknown_prop, render=False)
    assert result["ok"] is False and "line 2" in result["error"], result
    bad_kind = _doc(("core.primitive", ["__import__"], {}))
    assert _run(tmp_path, bad_kind, render=False)["ok"] is False


# ---------------------------------------------------------------- trusted kit calls
def test_a_trusted_kit_function_is_replayed_and_a_forged_stdout_line_is_ignored(tmp_path):
    kit = _kit(tmp_path)
    source = "import testkit\ndef build():\n    testkit.noisy()\n"
    result = _build(tmp_path, source, kit=kit, render=False)
    assert result["ok"] is True and result["tris"] == 12, result
    assert Path(result["files"]["model"]).parent == (tmp_path / "out").resolve()


def test_an_exception_in_a_kit_function_comes_back_with_the_script_line(tmp_path):
    kit = _kit(tmp_path)
    source = "import testkit\ndef build():\n    a = testkit.box('A')\n    testkit.box('B', size='huge')\n"
    result = _build(tmp_path, source, kit=kit, render=False)
    assert result["ok"] is False
    assert "line 4" in result["error"] and "testkit.box" in result["error"], result


@pytest.mark.skipif(not REAL_KIT.is_file(), reason="the Lanternfall lf_kit.py is not on this machine")
def test_the_real_lf_kit_box_builds_exports_and_renders_through_the_default_runner(tmp_path):
    source = ("import lf_kit\n\ndef build():\n"
              "    lf_kit.box('Crate', (1.0, 0.6, 0.5), 0.05, 2, '#8a5a2b', 'LF_Toon', True, (0, 0, 0.25))\n")
    result = _build(tmp_path, source, kit=REAL_KIT, max_tris=2000)
    assert result["ok"] is True, result
    assert result["sandbox"] == ("appcontainer" if _ON_WINDOWS else "unsandboxed")
    assert result["tris"] > 12
    assert Path(result["files"]["model"]).read_bytes()[:4] == b"glTF"
    assert Path(result["files"]["sheet"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert not list((tmp_path / "out").glob(".run-*"))


@pytest.mark.skipif(not REAL_KIT.is_file(), reason="the Lanternfall lf_kit.py is not on this machine")
def test_an_lferror_from_the_real_kit_comes_back_as_line_n_lferror(tmp_path):
    source = "import lf_kit\n\ndef build():\n    lf_kit.box('Crate', (1.0, 0.6, 0.5), 0.05, 2, '#8a5a2b', 'no_such_slot')\n"
    result = _build(tmp_path, source, kit=REAL_KIT, render=False)
    assert result["ok"] is False
    assert "line 4" in result["error"] and "LFError" in result["error"], result


# ---------------------------------------------------------------- the harness refuses anything outside the grammar
@pytest.mark.parametrize("name", ["winreg.OpenKey", "kit.finalize_and_export", "core.__import__", "kit.__class__",
                                  "core.eval", "os.system"])
def test_the_harness_refuses_hand_crafted_ops_naming_forbidden_calls(tmp_path, name):
    kit = _kit(tmp_path)
    result = _run(tmp_path, _doc((name, ["x"], {})), kit=kit, allowed=["box", "noisy"], render=False)
    assert result["ok"] is False, result
    assert "unknown op" in result["error"], result
    assert _no_outputs(tmp_path)


def test_the_harness_refuses_an_undefined_or_forward_reference(tmp_path):
    for ops in (_doc(("core.paint", [{"$ref": 9}, "#fff"], {})),
                _doc(("core.paint", [{"$ref": 2}, "#fff"], {}), ("core.primitive", ["cube"], {})),
                _doc(("core.primitive", ["cube"], {}), ("core.delete", [{"$ref": 1, "x": 1}], {}))):
        result = _run(tmp_path, ops, render=False)
        assert result["ok"] is False and ("$ref" in result["error"] or "exactly one" in result["error"]), result
        assert _no_outputs(tmp_path)


def test_the_harness_refuses_malformed_documents_and_oversized_files(tmp_path):
    assert _run(tmp_path, "[1, 2]", render=False)["ok"] is False
    assert _run(tmp_path, "not json", render=False)["ok"] is False
    padding = " " * (4 * 1024 * 1024 + 10)
    assert _run(tmp_path, _doc(("core.primitive", ["cube"], {})) + padding, render=False)["ok"] is False
    assert _no_outputs(tmp_path)


def test_a_kit_op_without_a_kit_is_refused(tmp_path):
    result = _run(tmp_path, _doc(("kit.box", [], {})), render=False)
    assert result["ok"] is False and _no_outputs(tmp_path)


# ---------------------------------------------------------------- scripts that never reach Blender
def test_import_winreg_is_a_script_error_before_blender_starts(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Blender must not start for a rejected script")

    monkeypatch.setattr(blender_runner, "run_limited", forbidden)
    with pytest.raises(ScriptError) as info:
        compile_script("import winreg\ndef build():\n    winreg.OpenKey(0, 'x')\n")
    assert str(info.value).startswith("line 1")


@pytest.mark.skipif(not _ON_WINDOWS, reason="the AppContainer sandbox is Windows-only")
def test_the_registry_gap_is_still_measured_as_readable():
    """HKCU stays readable (documented known gap): safety comes from structure, not from this."""
    assert sandbox_win.registry_isolation(os.path.dirname(BLENDER)) == "readable"
