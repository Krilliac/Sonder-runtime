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


def test_format_reflection_payload_is_rejected_and_leaks_nothing_even_without_the_gate(tmp_path, monkeypatch):
    from sonder_runtime.domain.meshgen.script_safety import check_script

    secret = "sk-ant-FAKE-live-key-do-not-leak"
    monkeypatch.setenv("SONDER_TEST_SECRET", secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    source = (
        "import bpy\ndef build():\n    leaked = \"{0.__globals__[os].environ}\".format(paint)\n"
        "    bpy.ops.mesh.primitive_cube_add()\n    raise ValueError(leaked[:400])\n"
    )
    assert check_script(source), "the gate must reject str.format reflection"
    result = _build(tmp_path, source, render=False)   # gate bypassed on purpose
    assert result["ok"] is False
    blob = repr(result)
    assert secret not in blob and "ANTHROPIC_API_KEY" not in blob and "SONDER_TEST_SECRET" not in blob


def test_the_injected_paint_helper_reaches_nothing_but_harmless_builtins(tmp_path):
    source = (
        "import bpy\ndef build():\n    g = paint.__globals__\n    bpy.ops.mesh.primitive_cube_add()\n"
        "    raise ValueError(','.join(sorted(g.keys())) + '|' + ','.join(sorted(g['__builtins__'].keys())))\n"
    )
    result = _build(tmp_path, source, render=False)   # gate bypassed on purpose
    keys, builtins_keys = result["error"].split(": ", 1)[1].split("|")
    assert set(keys.split(",")) == {"__builtins__", "paint"}
    assert set(builtins_keys.split(",")) == {"int", "len", "range", "str", "ValueError"}


def test_modifiers_are_baked_and_triangles_counted_after_them(tmp_path):
    from sonder_runtime.domain.meshgen.script_safety import check_script

    source = (
        "import bpy\n\ndef build():\n"
        "    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0.5))\n"
        "    box = bpy.context.active_object\n"
        "    bevel = box.modifiers.new(name='Bevel', type='BEVEL')\n"
        "    bevel.width = 0.05\n    bevel.segments = 2\n"
        "    sub = box.modifiers.new(name='Sub', type='SUBSURF')\n    sub.levels = 1\n"
        "    bpy.ops.object.shade_smooth()\n"
        "    paint(box, '#8899aa')\n"
    )
    assert check_script(source) == []
    result = _build(tmp_path, source, max_tris=5000, render=True)
    assert result["ok"] is True, result
    assert result["tris"] > 100, result["tris"]      # a bare cube is 12
    assert result["parts"] and result["parts"][0]["tris"] == result["tris"]
    assert Path(result["files"]["sheet"]).is_file()


def test_excluded_kit_names_are_stubbed_inside_blender(tmp_path):
    marker = tmp_path / "written.txt"
    (tmp_path / "kitsrc").mkdir()
    kit = tmp_path / "kitsrc" / "evilkit.py"
    kit.write_text(
        "import bpy\nMARKER = %r\n\ndef box():\n    bpy.ops.mesh.primitive_cube_add(size=1.0)\n\n"
        "def run_prop():\n    open(MARKER, 'w').write('pwned')\n" % str(marker), encoding="utf-8")
    script = "import bpy\nimport evilkit\n\ndef build():\n    evilkit.box()\n    evilkit.run_prop()\n"
    excluded = frozenset({"run_prop", "MARKER"})
    result = _build(tmp_path, script, kit_path=str(kit), kit_excluded=excluded, render=False)
    assert result["ok"] is False and "not available" in result["error"], result
    assert not marker.exists()
    allowed = "import bpy\nimport evilkit\n\ndef build():\n    evilkit.box()\n"
    good = _build(tmp_path, allowed, kit_path=str(kit), kit_excluded=excluded, render=False)
    assert good["ok"] is True and good["tris"] == 12, good
    assert not marker.exists()


REAL_KIT = Path(r"D:\LanternfallGame\Lanternfall\tools\blender\lf_kit.py")
_ON_WINDOWS = os.name == "nt"


def _excluded_for(kit):
    from sonder_runtime.domain.meshgen.kit_policy import analyse_kit

    return analyse_kit(kit.read_text(encoding="utf-8"))[1]


@pytest.mark.skipif(not REAL_KIT.is_file(), reason="the Lanternfall lf_kit.py is not on this machine")
def test_the_real_lf_kit_builds_a_box_through_the_default_sandboxed_runner(tmp_path):
    script = ("import bpy\nimport lf_kit\n\ndef build():\n"
              "    lf_kit.box('Crate', size=(1.0, 0.6, 0.5), radius=0.05)\n")
    result = _build(tmp_path, script, kit_path=str(REAL_KIT), kit_excluded=_excluded_for(REAL_KIT), max_tris=2000)
    assert result["ok"] is True, result
    assert result["sandbox"] == ("appcontainer" if _ON_WINDOWS else "unsandboxed")
    assert result["tris"] > 12
    assert Path(result["files"]["model"]).read_bytes()[:4] == b"glTF"
    assert Path(result["files"]["sheet"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert not list((tmp_path / "out").glob(".run-*"))  # per-build run dir and grant are gone


def _canary(tmp_path):
    folder = tmp_path / "canary"
    folder.mkdir()
    text = folder / "canary.txt"
    text.write_text("CANARY-READABLE", encoding="utf-8")
    obj = folder / "canary.obj"
    obj.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")
    return text, obj


def _escape(body):
    return "import bpy\nimport random\n\ndef build():\n" + "".join("    %s\n" % line for line in body.splitlines())


def _verdict(result):
    assert result["ok"] is False, result
    return result["error"]


def _fresh(tmp_path, label):
    folder = tmp_path / label
    folder.mkdir()
    return folder


_REAL_IMPORT = ("raw = random.Random.__init__.__globals__['__builtins__']\n"
                "load = raw['__import__'] if isinstance(raw, dict) else raw.__import__\n")


@pytest.mark.skipif(not _ON_WINDOWS, reason="the AppContainer sandbox is Windows-only")
def test_the_sandbox_denies_reading_a_file_outside_the_run_dir_and_unsandboxed_would_not(tmp_path):
    text, _ = _canary(tmp_path)
    script = _escape("try:\n    bpy.data.texts.load(%r)\nexcept Exception:\n    raise ValueError('READ-DENIED')\n"
                     "raise ValueError('READ-OK')" % str(text))
    assert "READ-DENIED" in _verdict(_build(_fresh(tmp_path, "boxed"), script, render=False))
    # control: without the sandbox the same script reads the canary, so the test discriminates
    assert "READ-OK" in _verdict(_build(_fresh(tmp_path, "free"), script, render=False, sandbox="unsandboxed"))


@pytest.mark.skipif(not _ON_WINDOWS, reason="the AppContainer sandbox is Windows-only")
def test_the_sandbox_denies_loopback_network_and_unsandboxed_would_not(tmp_path):
    import socket
    import threading

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    accepted = []
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except OSError:
                continue
            accepted.append(1)
            connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        script = _escape(_REAL_IMPORT + "link = load('socket')\n"
                         "try:\n    link.create_connection(('127.0.0.1', %d), timeout=3).close()\n"
                         "except Exception:\n    raise ValueError('NET-DENIED')\nraise ValueError('NET-OK')" % port)
        assert "NET-DENIED" in _verdict(_build(_fresh(tmp_path, "boxed"), script, render=False))
        assert accepted == []
        assert "NET-OK" in _verdict(_build(_fresh(tmp_path, "free"), script, render=False, sandbox="unsandboxed"))
        assert accepted
    finally:
        stop.set()
        thread.join(2)
        listener.close()


@pytest.mark.skipif(not _ON_WINDOWS, reason="the AppContainer sandbox is Windows-only")
def test_obj_import_of_a_file_outside_the_run_dir_yields_no_faces_in_the_sandbox(tmp_path):
    _, obj = _canary(tmp_path)
    script = _escape("faces = 0\ntry:\n    bpy.ops.wm.obj_import(filepath=%r)\n"
                     "    faces = sum(len(m.polygons) for m in bpy.data.meshes)\nexcept Exception:\n    faces = 0\n"
                     "raise ValueError('FACES=%%d' %% faces)" % str(obj))
    assert "FACES=0" in _verdict(_build(_fresh(tmp_path, "boxed"), script, render=False))
    assert "FACES=1" in _verdict(_build(_fresh(tmp_path, "free"), script, render=False, sandbox="unsandboxed"))


@pytest.mark.skipif(not _ON_WINDOWS, reason="the AppContainer sandbox is Windows-only")
def test_the_job_memory_limit_is_reported_as_such(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_MESHGEN_MEM_MB", "512")
    script = _escape("blob = [0] * 400000000\nblob[::4096] = [1] * len(blob[::4096])")
    result = _build(tmp_path, script, render=False)
    assert result["ok"] is False and "memory limit exceeded (512 MB)" in result["error"], result
