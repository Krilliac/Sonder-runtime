"""Script safety gate for model-written Blender build scripts."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.meshgen.script_safety import MAX_SOURCE_CHARS, check_script, kit_public_names

GOOD = '''import bpy
import bmesh
import math
from mathutils import Vector

def build():
    for i in range(3):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=(i, 0, 0.5))
        obj = bpy.context.active_object
        obj.rotation_euler = (0, math.radians(10), 0)
        obj.scale = Vector((1, 2, 1))
        paint(obj, "#ff0000")
        bpy.ops.object.modifier_apply(modifier="Bevel")
    mesh = bpy.data.meshes.new("m")
    ob = bpy.data.objects.new("o", mesh)
    bpy.context.scene.collection.objects.link(ob)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.to_mesh(mesh)
    bm.free()
    mat = bpy.data.materials.new("mat")
    if ob.type == "MESH":
        ob.data.materials.append(mat)
    dg = bpy.context.evaluated_depsgraph_get()
    bpy.context.view_layer.update()
'''

KIT = '''"""kit"""
import os
PALETTE = {"a": "#fff"}

def wheel(r):
    """Make a wheel."""

def _private():
    pass

class Part:
    pass
'''


def _joined(reasons):
    return " | ".join(reasons)


def test_accepts_a_normal_build_script():
    assert check_script(GOOD) == []


def test_accepts_a_throwaway_underscore_loop_variable():
    assert check_script("import bpy\ndef build():\n    for _ in range(2):\n        pass\n") == []


def test_kit_public_names_exclude_imports_and_private_names():
    assert kit_public_names(KIT) == {"PALETTE", "wheel", "Part"}
    assert kit_public_names("def (broken") == frozenset()


def test_accepts_kit_use_only_through_public_names_and_aliases():
    names = kit_public_names(KIT)
    plain = "import bpy\nimport propkit\n\ndef build():\n    propkit.wheel(propkit.PALETTE['a'])\n"
    aliased = "import propkit as pk\nfrom propkit import wheel\n\ndef build():\n    pk.wheel(1)\n    wheel(2)\n"
    assert check_script(plain, "propkit", names) == []
    assert check_script(aliased, "propkit", names) == []
    assert check_script(plain, None, None)  # no kit named: the import itself is refused


@pytest.mark.parametrize("body,needle", [
    ("import propkit\ndef build():\n    propkit.os.system('x')\n", "kit"),
    ("import propkit\ndef build():\n    propkit._private()\n", "_private"),
    ("import propkit\ndef build():\n    x = propkit\n", "kit"),
    ("import propkit\ndef build():\n    f(propkit)\n", "kit"),
    ("import propkit as pk\ndef build():\n    pk.sys\n", "kit"),
    ("from propkit import os\ndef build():\n    pass\n", "not a public name"),
    ("from propkit import *\ndef build():\n    pass\n", "not a public name"),
])
def test_kit_attribute_access_is_limited_to_public_defs(body, needle):
    reasons = check_script(body, "propkit", kit_public_names(KIT))
    assert reasons and needle in _joined(reasons), reasons


def test_a_kit_module_without_names_allows_no_attribute():
    assert check_script("import propkit\ndef build():\n    propkit.wheel(1)\n", "propkit")


@pytest.mark.parametrize("source,line,needle", [
    ("import bpy\nimport os\n\ndef build():\n    pass\n", 2, "os"),
    ("import bpy\nimport subprocess\n\ndef build():\n    pass\n", 2, "subprocess"),
    ("import bpy\nfrom os import path\n\ndef build():\n    pass\n", 2, "os"),
    ("import bpy\n\ndef build():\n    open('x')\n", 4, "open"),
    ("import bpy\n\ndef build():\n    x = bpy.__class__\n", 4, "__class__"),
    ("import bpy\n\ndef build():\n    y = getattr(bpy, 'ops')\n", 4, "getattr"),
    ("import bpy\n\ndef build():\n    exec('1')\n", 4, "exec"),
    ("import bpy\n\ndef build():\n    bpy.ops.wm.quit_blender()\n", 4, "bpy.ops"),
    ("import bpy\n\ndef build():\n    bpy.app.handlers.load_post.append(None)\n", 4, ".app"),
    ("import bpy\n\ndef build():\n    bpy.context.preferences.view.ui_scale = 2\n", 4, "preferences"),
    ("import bpy\n\ndef build():\n    bpy.ops.export_scene.fbx(filepath='x')\n", 4, "export_scene"),
    ("import bpy\nfrom . import sibling\n\ndef build():\n    pass\n", 2, "relative"),
    ("import bpy\n\ndef build(_x=1):\n    pass\n", 3, "_x"),
])
def test_rejects_with_a_reason_naming_the_line(source, line, needle):
    reasons = check_script(source)
    assert reasons, source
    assert any(r.startswith("line %d:" % line) and needle in r for r in reasons), reasons


# Each of these passed the previous, deny-chain gate (or is a close variant).  The first was run
# in real Blender 5.2 and executed arbitrary code with real os/sys.
EXPLOITS = {
    "text_as_module": (
        "import bpy\ndef build():\n    bpy.ops.mesh.primitive_cube_add()\n"
        "    t = bpy.context.blend_data.texts.new('x.py'); t.write(\"import os\\nprint('ESCAPED')\\n\"); t.as_module()\n"),
    "alias_ops": "import bpy\ndef build():\n    o = bpy.ops\n    o.wm.save_as_mainfile(filepath='x')\n",
    "alias_bpy": "import bpy\ndef build():\n    b = bpy\n    b.utils.execfile('x.py')\n",
    "alias_data": "import bpy\ndef build():\n    d = bpy.data\n    d.libraries.write('x')\n",
    "list_laundering": "import bpy\ndef build():\n    [bpy.ops][0].text.run_script()\n",
    "dict_laundering": "import bpy\ndef build():\n    m = {'k': bpy}\n    m['k'].app\n",
    "bpy_as_argument": "import bpy\ndef f(x):\n    return x\ndef build():\n    f(bpy).utils\n",
    "bpy_as_default": "import bpy\ndef build(m=bpy):\n    pass\n",
    "bpy_returned": "import bpy\ndef g():\n    return bpy\ndef build():\n    g()\n",
    "bpy_import_alias": "import bpy as b\ndef build():\n    b.ops.mesh.primitive_cube_add()\n",
    "bpy_from_import": "from bpy import ops\ndef build():\n    ops.wm.quit_blender()\n",
    "bpy_submodule_import": "import bpy.utils\ndef build():\n    bpy.utils.execfile('x')\n",
    "image_save": "import bpy\ndef build():\n    img = bpy.data.images.new('i', 4, 4)\n    img.filepath_raw = 'C:/x.png'\n    img.save()\n",
    "mesh_save": "import bpy\ndef build():\n    o = bpy.context.object\n    o.data.save()\n",
    "driver_expression": (
        "import bpy\ndef build():\n    o = bpy.context.object\n    d = o.driver_add('location', 0)\n"
        "    d.driver.expression = '__import__(\"os\")'\n"),
    "context_copy_lookup": "import bpy\ndef build():\n    c = bpy.context.copy()\n    c['blend_data'].texts\n",
    "string_data_block": "import bpy\ndef build():\n    k = 'texts'\n",
    "window_manager": "import bpy\ndef build():\n    bpy.context.window_manager.popup_menu(None)\n",
    "ops_text_category": "import bpy\ndef build():\n    bpy.ops.text.run_script()\n",
    "ops_script_category": "import bpy\ndef build():\n    bpy.ops.script.python_file_run(filepath='x')\n",
    "ops_not_called": "import bpy\ndef build():\n    f = bpy.ops.object.join\n    f()\n",
    "ops_bake_operator": "import bpy\ndef build():\n    bpy.ops.object.bake_image()\n",
    "ops_load_operator": "import bpy\ndef build():\n    bpy.ops.object.load_reference_image(filepath='x')\n",
    "filepath_keyword": "import bpy\ndef build():\n    bpy.ops.mesh.primitive_cube_add(filepath='x')\n",
    "library_write": "import bpy\ndef build():\n    bpy.data.objects['a'].library.write()\n",
    "types_subclass": "import bpy\nclass Op(bpy.types.Operator):\n    pass\ndef build():\n    pass\n",
    "id_data": "import bpy\ndef build():\n    bpy.context.object.id_data.rna_type\n",
    "data_unlisted_collection": "import bpy\ndef build():\n    bpy.data.images\n",
    "context_unlisted_member": "import bpy\ndef build():\n    bpy.context.screen\n",
    "bpy_rebound": "import bpy\ndef build():\n    bpy = 1\n",
    "bpy_parameter": "import bpy\ndef build(bpy):\n    pass\n",
    "kit_os": "import propkit\ndef build():\n    propkit.os.system('x')\n",
}


@pytest.mark.parametrize("name", sorted(EXPLOITS))
def test_known_exploits_are_rejected(name):
    reasons = check_script(EXPLOITS[name], "propkit", kit_public_names(KIT))
    assert reasons, "exploit %s was accepted" % name
    assert all(r.startswith("line ") or r.startswith("no top-level") for r in reasons)


def test_every_exploit_is_rejected_without_a_kit_too():
    for name, source in EXPLOITS.items():
        assert check_script(source), name


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


FORMAT_EXPLOITS = {
    "format_globals_literal": (
        "import bpy\ndef build():\n    leaked = \"{0.__globals__[os].environ}\".format(paint)\n"
        "    bpy.ops.mesh.primitive_cube_add()\n    raise ValueError(leaked[:400])\n"),
    "format_map": "import bpy\ndef build():\n    x = '{p.__globals__}'.format_map({'p': paint})\n",
    "format_plain": "import bpy\ndef build():\n    x = '{0}'.format(1)\n",
    "dunder_literal_alone": "import bpy\ndef build():\n    x = '{0.__globals__}'\n",
    "dunder_bytes": "import bpy\ndef build():\n    x = b'__class__'\n",
    "format_builtin": "import bpy\ndef build():\n    x = format(1, 'd')\n",
    "kit_globals_via_format": "import propkit\ndef build():\n    x = '{0.__globals__}'.format(propkit.wheel)\n",
}


@pytest.mark.parametrize("name", sorted(FORMAT_EXPLOITS))
def test_str_format_reflection_is_rejected(name):
    assert check_script(FORMAT_EXPLOITS[name], "propkit", kit_public_names(KIT)), name


def test_percent_and_fstring_formatting_stay_available():
    source = ("import bpy\ndef build():\n    n = 3\n    label = 'part %d of %s' % (n, 'cart')\n"
              "    other = f'{n:03d}-{label}'\n    bpy.ops.mesh.primitive_cube_add()\n")
    assert check_script(source) == []


QUALITY_SCRIPT = '''import bpy
import bmesh
import math

def build():
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0.5))
    box = bpy.context.active_object
    bevel = box.modifiers.new(name="Bevel", type="BEVEL")
    bevel.width = 0.05
    bevel.segments = 3
    bevel.limit_method = "ANGLE"
    sub = box.modifiers.new(name="Sub", type="SUBSURF")
    sub.levels = 1
    sub.render_levels = 1
    box.modifiers.new(name="Wn", type="WEIGHTED_NORMAL")
    box.modifiers.new(name="Tri", type="TRIANGULATE")
    mirror = box.modifiers.new(name="Mir", type="MIRROR")
    mirror.use_axis[0] = True
    arr = box.modifiers.new(name="Arr", type="ARRAY")
    arr.count = 1
    solid = box.modifiers.new(name="Sol", type="SOLIDIFY")
    solid.thickness = 0.01
    dec = box.modifiers.new(name="Dec", type="DECIMATE")
    dec.ratio = 1.0
    bpy.ops.object.shade_smooth()
    bpy.ops.object.shade_flat()
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.subdivide(number_cuts=1)
    bpy.ops.mesh.inset(thickness=0.02)
    bpy.ops.object.mode_set(mode="OBJECT")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=0.5)
    bmesh.ops.bevel(bm, geom=list(bm.edges), offset=0.02, segments=2, affect="EDGES")
    bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=1)
    mesh = bpy.data.meshes.new("extra")
    bm.to_mesh(mesh)
    bm.free()
    other = bpy.data.objects.new("extra", mesh)
    bpy.context.scene.collection.objects.link(other)
    cut = other.modifiers.new(name="Cut", type="BOOLEAN")
    cut.operation = "DIFFERENCE"
    cut.object = box
    paint(box, "#8899aa")
    paint(other, "#aa9988")
'''


def test_quality_modifiers_shading_and_bmesh_ops_pass_the_gate():
    assert check_script(QUALITY_SCRIPT) == []


def _mod(call):
    return "import bpy\n\ndef build():\n    obj = bpy.context.active_object\n    %s\n" % call


def test_modifier_types_must_be_allow_listed_literals():
    assert check_script(_mod("obj.modifiers.new('m', 'SUBSURF')")) == []
    assert check_script(_mod("obj.modifiers.new(name='m', type='BEVEL')")) == []
    for call in ("obj.modifiers.new('m', 'NODES')", "obj.modifiers.new('m', 'DISPLACE')",
                 "kind = 'BEVEL'\n    obj.modifiers.new('m', kind)", "obj.modifiers.new('m', 'BE' + 'VEL')",
                 "obj.modifiers.new('m')"):
        assert check_script(_mod(call)), call


def test_splat_arguments_are_refused_on_bpy_calls():
    assert check_script(_mod("bpy.ops.mesh.primitive_cube_add(**{'size': 1})"))
    assert check_script(_mod("bpy.ops.mesh.primitive_cube_add(*[1.0])"))
    assert check_script(_mod("obj.modifiers.new(**{'name': 'x', 'type': 'NODES'})"))


def test_path_resolve_and_node_group_operators_are_refused():
    assert check_script(_mod("obj.path_resolve('data')"))
    assert check_script(_mod("bpy.ops.object.modifier_add(type='NODES')"))
    assert check_script(_mod("bpy.ops.node.add_node()"))


def test_literal_size_caps_stop_single_argument_memory_bombs():
    assert check_script(_mod("bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16)")) == []
    assert check_script(_mod("bpy.ops.mesh.primitive_uv_sphere_add(segments=100000000)"))
    assert check_script(_mod("obj.modifiers.new('m', 'SUBSURF').levels = 12"))
    assert check_script(_mod("m = obj.modifiers.new('m', 'ARRAY')\n    m.count = 10_000_000"))
