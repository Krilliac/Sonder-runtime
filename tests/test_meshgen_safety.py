"""Script safety gate for model-written Blender build scripts."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.meshgen.script_safety import MAX_SOURCE_CHARS, check_script

GOOD = '''import bpy
import math
from mathutils import Vector

def build():
    for i in range(3):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(i, 0, 0.5))
        obj = bpy.context.active_object
        obj.rotation_euler = (0, math.radians(10), 0)
        paint(obj, "#ff0000")
        bpy.ops.object.modifier_apply(modifier="Bevel")
'''


def _joined(reasons):
    return " | ".join(reasons)


def test_accepts_a_normal_build_script():
    assert check_script(GOOD) == []


def test_accepts_a_throwaway_underscore_loop_variable():
    assert check_script("import bpy\ndef build():\n    for _ in range(2):\n        pass\n") == []


def test_accepts_kit_import_only_when_named():
    source = "import bpy\nimport propkit\nfrom propkit import wheel\n\ndef build():\n    wheel()\n"
    assert check_script(source, "propkit") == []
    reasons = check_script(source, None)
    assert reasons and all(r.startswith("line ") for r in reasons)
    assert "propkit" in _joined(reasons)


@pytest.mark.parametrize("source,line,needle", [
    ("import bpy\nimport os\n\ndef build():\n    pass\n", 2, "os"),
    ("import bpy\nimport subprocess\n\ndef build():\n    pass\n", 2, "subprocess"),
    ("import bpy\nfrom os import path\n\ndef build():\n    pass\n", 2, "os"),
    ("import bpy\n\ndef build():\n    open('x')\n", 4, "open"),
    ("import bpy\n\ndef build():\n    x = bpy.__class__\n", 4, "__class__"),
    ("import bpy\n\ndef build():\n    y = getattr(bpy, 'ops')\n", 4, "getattr"),
    ("import bpy\n\ndef build():\n    exec('1')\n", 4, "exec"),
    ("import bpy\n\ndef build():\n    bpy.ops.wm.quit_blender()\n", 4, "bpy.ops.wm"),
    ("import bpy\n\ndef build():\n    bpy.app.handlers.load_post.append(None)\n", 4, "bpy.app"),
    ("import bpy\n\ndef build():\n    bpy.context.preferences.view.ui_scale = 2\n", 4, "preferences"),
    ("import bpy\n\ndef build():\n    bpy.ops.export_scene.fbx(filepath='x')\n", 4, "export_scene"),
    ("import bpy\nfrom . import sibling\n\ndef build():\n    pass\n", 2, "relative"),
    ("import bpy\n\ndef build(_x=1):\n    pass\n", 3, "_x"),
])
def test_rejects_with_a_reason_naming_the_line(source, line, needle):
    reasons = check_script(source)
    assert reasons, source
    assert any(r.startswith("line %d:" % line) and needle in r for r in reasons), reasons


def test_rejects_through_import_aliases():
    reasons = check_script("import bpy as b\nfrom bpy import app\n\ndef build():\n    b.ops.wm.quit_blender()\n    app.handlers\n")
    text = _joined(reasons)
    assert "line 5" in text and "line 6" in text


def test_rejects_missing_build_function():
    reasons = check_script("import bpy\n\ndef make():\n    pass\n")
    assert any("build" in r for r in reasons)
    assert check_script("import bpy\nclass A:\n    def build(self):\n        pass\n")


def test_rejects_syntax_error_with_line():
    reasons = check_script("import bpy\ndef build(:\n    pass\n")
    assert len(reasons) == 1 and reasons[0].startswith("line 2:") and "syntax" in reasons[0]


def test_rejects_empty_and_oversized_sources():
    assert check_script("   ") == ["script is empty"]
    big = "x = 1\n" * (MAX_SOURCE_CHARS // 3)
    assert "limit" in _joined(check_script(big))
