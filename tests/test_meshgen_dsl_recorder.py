"""The build-script recorder: model code is interpreted on the host and never executes.

Covers realistic scripts (expressiveness), determinism, the adversarial corpus (every earlier
exploit of the old AST gate plus the generator-frame walk that broke it), the closed-value-domain
property (P1), the static no-generic-dispatch property (P2), the resource caps and the messages
the model sees.
"""
from __future__ import annotations

import ast
import json
import os
import random
import re
import time
from pathlib import Path

import pytest

from sonder_runtime.domain.meshgen.dsl import opschema, recorder
from sonder_runtime.domain.meshgen.dsl.errors import ScriptError
from sonder_runtime.domain.meshgen.dsl.manifest import build_manifest
from sonder_runtime.domain.meshgen.dsl.recorder import compile_script
from sonder_runtime.domain.meshgen.dsl.values import is_domain
from sonder_runtime.domain.meshgen.prompts import repair_hint

KIT = '''"""kit"""
import os
PALETTE = {"red": "#cc2222", "blue": "#2222cc"}

def box(name, size, radius=0.0, hexcol="#888888", loc=(0, 0, 0), rot=(0, 0, 0)):
    """A box."""

def wheel(name, r, w=0.1, loc=(0, 0, 0)):
    """A wheel."""

def tint(obj, predicate, hexcol="#ffffff"):
    """Colour the faces the predicate selects."""
    return predicate(obj)

def _private():
    pass
'''
MANIFEST = build_manifest(KIT, "propkit")
LINE = re.compile(r"^line \d+")


def rec(source, manifest=MANIFEST, seed=0):
    return compile_script(source, manifest, seed=seed)


def reject(source, manifest=MANIFEST):
    with pytest.raises(ScriptError) as info:
        compile_script(source, manifest)
    return str(info.value)


# ---------------------------------------------------------------- expressiveness
CART = '''import math
import propkit

def wheel_at(x, y, name):
    w = propkit.wheel(name, 0.3, 0.1, loc=(x, y, 0.3))
    transform(w, rot=(0, 90, 0))
    return w

def build():
    body = propkit.box("Body", (2.0, 1.0, 0.4), 0.05, propkit.PALETTE["red"], loc=(0, 0, 0.5))
    parts = [body]
    for i, (x, y) in enumerate([(0.8, 0.6), (0.8, -0.6), (-0.8, 0.6), (-0.8, -0.6)]):
        parts.append(wheel_at(x, y, "Wheel%d" % i))
    join(parts, "Cart")
'''

TOWER = '''import math

def build():
    base = primitive("cylinder", "Base", radius=1.0, depth=0.5, segments=16, loc=(0, 0, 0.25))
    parts = [base]
    for level in range(4):
        r = 0.9 - level * 0.15
        z = 0.5 + level * 0.8
        ring = primitive("cylinder", "Level%d" % level, radius=r, depth=0.8, segments=16, loc=(0, 0, z + 0.4))
        modifier(ring, "BEVEL", width=0.03, segments=2)
        paint(ring, "#%02x%02x%02x" % (100 + level * 20, 100, 120))
        parts.append(ring)
    roof = primitive("cone", "Roof", radius=0.7, depth=1.0, segments=16, loc=(0, 0, 4.2))
    paint(roof, "#aa3322")
    parts.append(roof)
    join(parts, "Tower")
'''

PYRAMID = '''def build():
    verts = [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0), (0, 0, 1.5)]
    faces = [(0, 1, 2, 3), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
    p = mesh("Pyramid", verts, faces)
    shade_smooth(p, False)
    paint_faces(p, lambda f: f.center.z > 0.4, "#ddcc88")
    paint(p, "#887744")
'''

TREE = '''import math
import random

def crown(n, radius, z):
    out = []
    for k in range(n):
        a = 2 * math.pi * k / n
        out.append(primitive("ico_sphere", "Leaf%d" % k, radius=0.25, subdivisions=1,
                             loc=(radius * math.cos(a), radius * math.sin(a), z)))
    return out

def build():
    random.seed(7)
    trunk = primitive("cylinder", "Trunk", radius=0.15, depth=1.5, segments=8, loc=(0, 0, 0.75))
    paint(trunk, "#553311")
    parts = [trunk] + crown(6, 0.5, 1.8) + crown(4, 0.25, 2.2)
    for leaf in parts[1:]:
        paint(leaf, "#2%d6622" % random.randint(0, 9))
    join(parts, "Tree")
'''

TABLE = '''import propkit

LEG = 0.08

def leg(x, y):
    return propkit.box("Leg", (LEG, LEG, 0.7), loc=(x, y, 0.35))

def build():
    top = propkit.box("Top", (1.4, 0.8, 0.06), 0.01, "#996633", loc=(0, 0, 0.73))
    legs = [leg(sx * 0.62, sy * 0.32) for sx in (-1, 1) for sy in (-1, 1)]
    join([top] + legs, "Table")
'''

BARREL = '''import math

def build():
    b = primitive("cylinder", "Barrel", radius=0.4, depth=1.0, segments=12, loc=(0, 0, 0.5))
    modifier(b, "SUBSURF", levels=1)
    shade_smooth(b)
    hoops = []
    for z in (0.2, 0.8):
        h = primitive("torus", "Hoop", major_radius=0.42, minor_radius=0.03, segments=12, loc=(0, 0, z))
        paint(h, "#444444")
        hoops.append(h)
    paint(b, "#8a5a2b")
    result = join([b] + hoops, "Barrel")
    delete_me = primitive("cube", "Scratch")
    delete(delete_me)
'''

SCRIPTS = {"cart": CART, "tower": TOWER, "pyramid": PYRAMID, "tree": TREE, "table": TABLE, "barrel": BARREL}


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_realistic_scripts_record_and_validate(name):
    result = rec(SCRIPTS[name])
    assert result.ops, name
    opschema.validate_ops(json.loads(result.text), MANIFEST.exposed)
    for op in result.ops:
        assert op["op"].split(".", 1)[0] in ("kit", "core")
        assert LINE.match("line %d" % op["line"])


def test_cart_golden_ops():
    result = rec(CART)
    names = [op["op"] for op in result.ops]
    assert names == ["kit.box"] + ["kit.wheel", "core.transform"] * 4 + ["core.join"]
    box = result.ops[0]
    assert box["args"][0] == "Body" and box["args"][1] == [2.0, 1.0, 0.4]
    assert box["args"][3] == "#cc2222"  # a kit constant folded in as plain data
    join_op = result.ops[-1]
    assert join_op["args"][0] == [{"$ref": i} for i in (1, 2, 4, 6, 8)]
    assert join_op["args"][1] == "Cart"
    assert result.ops[2]["kwargs"] == {"rot": [0, 90, 0]}


def test_pyramid_golden_predicate():
    result = rec(PYRAMID)
    names = [op["op"] for op in result.ops]
    assert names == ["core.mesh", "core.shade_smooth", "core.paint_faces", "core.paint"]
    pred = result.ops[2]["args"][1]
    assert set(pred) == {"$pred"}
    assert json.loads(json.dumps(pred)) == pred


def test_predicate_param_of_a_kit_function_is_serialised():
    result = rec("import propkit\ndef build():\n    a = propkit.box('A', (1, 1, 1))\n"
                 "    limit = 0.5\n    propkit.tint(a, lambda p: p.center.z > limit, '#fff')\n")
    assert set(result.ops[1]["args"][1]) == {"$pred"}


def test_print_goes_to_the_log_and_handles_are_printable():
    result = rec("def build():\n    a = primitive('cube')\n    print('hi', 3, a)\n")
    assert result.log and result.log[0].startswith("hi 3")


def test_comprehensions_helpers_fstrings_and_percent_formatting():
    src = ("def name(i):\n    return f'part-{i:02d}'\n\ndef build():\n"
           "    xs = [i * 0.5 for i in range(4) if i % 2 == 0]\n"
           "    d = {'a': 1, 'b': 2}\n    total = sum(d.values()) + sum(xs)\n"
           "    parts = [primitive('cube', name(i), loc=(x, 0, 0)) for i, x in enumerate(xs)]\n"
           "    label = 'n=%d t=%.1f' % (len(parts), total)\n    join(parts, label)\n")
    result = rec(src)
    assert result.ops[-1]["args"][1] == "n=2 t=4.0"


# ---------------------------------------------------------------- determinism
def test_recording_is_deterministic_and_independent_of_the_environment(tmp_path, monkeypatch):
    canary = "CANARY-ENV-0123456789abcdef"
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secret.txt").write_text("CANARY-FILE-fedcba9876543210", encoding="utf-8")
    first = {name: rec(src, seed=3).text for name, src in SCRIPTS.items()}
    monkeypatch.setenv("SONDER_CANARY", canary)
    monkeypatch.setenv("PATH", os.environ.get("PATH", "") + os.pathsep + canary)
    second = {name: rec(src, seed=3).text for name, src in SCRIPTS.items()}
    assert first == second
    for text in second.values():
        assert canary not in text and "CANARY-FILE" not in text


def test_random_is_seeded_by_the_recording_seed_only():
    src = "import random\ndef build():\n    primitive('cube', loc=(random.random(), 0, 0))\n"
    assert rec(src, seed=1).text == rec(src, seed=1).text
    assert rec(src, seed=1).text != rec(src, seed=2).text


def test_a_script_cannot_read_the_environment_or_files():
    for src in ("import os\ndef build():\n    pass\n", "def build():\n    open('secret.txt')\n",
                "def build():\n    x = os.environ\n"):
        assert LINE.match(reject(src))


# ---------------------------------------------------------------- adversarial corpus
OLD_EXPLOITS = {
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
    "driver_expression": (
        "import bpy\ndef build():\n    o = bpy.context.object\n    d = o.driver_add('location', 0)\n"
        "    d.driver.expression = '__import__(\"os\")'\n"),
    "context_copy_lookup": "import bpy\ndef build():\n    c = bpy.context.copy()\n    c['blend_data'].texts\n",
    "window_manager": "import bpy\ndef build():\n    bpy.context.window_manager.popup_menu(None)\n",
    "ops_text_category": "import bpy\ndef build():\n    bpy.ops.text.run_script()\n",
    "ops_script_category": "import bpy\ndef build():\n    bpy.ops.script.python_file_run(filepath='x')\n",
    "types_subclass": "import bpy\nclass Op(bpy.types.Operator):\n    pass\ndef build():\n    pass\n",
    "id_data": "import bpy\ndef build():\n    bpy.context.object.id_data.rna_type\n",
    "bpy_rebound": "import bpy\ndef build():\n    bpy = 1\n",
    "bpy_parameter": "import bpy\ndef build(bpy):\n    pass\n",
    "kit_os": "import propkit\ndef build():\n    propkit.os.system('x')\n",
}

FORMAT_EXPLOITS = {
    "format_globals_literal": (
        "def build():\n    leaked = \"{0.__globals__[os].environ}\".format(paint)\n"
        "    primitive('cube')\n    raise ValueError(leaked[:400])\n"),
    "format_map": "def build():\n    x = '{p.__globals__}'.format_map({'p': paint})\n",
    "format_plain": "def build():\n    x = '{0}'.format(1)\n",
    "dunder_bytes": "def build():\n    x = b'__class__'\n",
    "format_builtin": "def build():\n    x = format(1, 'd')\n",
    "kit_globals_via_format": "import propkit\ndef build():\n    x = '{0.__globals__}'.format(propkit.wheel)\n",
}

NEW_EXPLOITS = {
    "generator_frame_walk": (
        "def g():\n    yield 1\ndef build():\n    f = g().gi_frame.f_back.f_globals\n"),
    "gi_frame": "def build():\n    x = (i for i in range(3))\n    y = x.gi_frame\n",
    "split_winreg": "def build():\n    m = __import__('win' + 'reg')\n",
    "dunder_import": "def build():\n    __import__('winreg')\n",
    "import_winreg": "import winreg\ndef build():\n    pass\n",
    "import_inside_build": "def build():\n    import os\n",
    "builtins_name": "def build():\n    x = __builtins__\n",
    "builtins_attr": "def build():\n    x = primitive.__globals__\n",
    "globals_call": "def build():\n    g = globals()\n",
    "locals_call": "def build():\n    g = locals()\n",
    "vars_call": "def build():\n    g = vars()\n",
    "type_call": "def build():\n    t = type(1)\n",
    "type_base": "def build():\n    t = (1).__class__.__bases__\n",
    "getattr_call": "def build():\n    getattr(1, 'real')\n",
    "setattr_call": "def build():\n    setattr(1, 'real', 2)\n",
    "dunder_class": "def build():\n    x = 'a'.__class__\n",
    "dunder_dict": "def build():\n    x = {}.__dict__\n",
    "eval_call": "def build():\n    eval('1')\n",
    "exec_call": "def build():\n    exec('1')\n",
    "compile_call": "def build():\n    compile('1', 'x', 'exec')\n",
    "open_call": "def build():\n    open('secret.txt').read()\n",
    "input_call": "def build():\n    input()\n",
    "lambda_called_directly": "def build():\n    (lambda: 1)()\n",
    "lambda_assigned": "def build():\n    f = lambda: 1\n",
    "lambda_in_helper_arg": "def build():\n    sorted([3, 1], key=lambda v: v)\n",
    "kit_function_as_value": "import propkit\ndef build():\n    f = propkit.box\n",
    "kit_function_in_list": "import propkit\ndef build():\n    fs = [propkit.box]\n",
    "core_function_as_value": "def build():\n    f = primitive\n    f('cube')\n",
    "kit_os_attr": "import propkit\ndef build():\n    propkit.os.system('x')\n",
    "kit_private": "import propkit\ndef build():\n    propkit._private()\n",
    "class_def": "class A:\n    pass\ndef build():\n    pass\n",
    "class_in_build": "def build():\n    class A:\n        pass\n",
    "try_stmt": "def build():\n    try:\n        pass\n    except Exception:\n        pass\n",
    "with_stmt": "def build():\n    with open('x') as f:\n        pass\n",
    "global_stmt": "x = 1\ndef build():\n    global x\n",
    "nonlocal_stmt": "def build():\n    a = 1\n    def f():\n        nonlocal a\n",
    "raise_stmt": "def build():\n    raise ValueError('x')\n",
    "assert_stmt": "def build():\n    assert False\n",
    "del_stmt": "def build():\n    a = 1\n    del a\n",
    "star_args": "def f(*a):\n    pass\ndef build():\n    pass\n",
    "kwargs": "def f(**k):\n    pass\ndef build():\n    pass\n",
    "decorator": "@staticmethod\ndef f():\n    pass\ndef build():\n    pass\n",
    "async_def": "async def build():\n    pass\n",
    "await_expr": "def build():\n    await x\n",
    "yield_expr": "def build():\n    yield 1\n",
    "walrus": "def build():\n    (x := 1)\n",
    "set_literal": "def build():\n    s = {1, 2}\n",
    "set_builtin": "def build():\n    s = set()\n",
    "bit_ops": "def build():\n    x = 1 << 100\n",
    "matmul": "def build():\n    x = 1 @ 2\n",
    "ellipsis": "def build():\n    x = ...\n",
    "complex_literal": "def build():\n    x = 1j\n",
    "bytes_literal": "def build():\n    x = b'abc'\n",
    "slice_dunder": "def build():\n    x = [1][0].__index__\n",
    "handle_attr": "def build():\n    a = primitive('cube')\n    a.location\n",
    "handle_dunder": "def build():\n    a = primitive('cube')\n    a.__class__\n",
    "handle_index": "def build():\n    a = primitive('cube')\n    a[0]\n",
    "handle_arith": "def build():\n    a = primitive('cube')\n    b = a + 1\n",
    "handle_compare": "def build():\n    a = primitive('cube')\n    if a > 1:\n        pass\n",
    "handle_unpack": "def build():\n    a, b = primitive('cube')\n",
    "handle_len": "def build():\n    a = primitive('cube')\n    len(a)\n",
    "handle_in_fstring": "def build():\n    a = primitive('cube')\n    s = f'{a.x}'\n",
    "nested_lambda_handle": (
        "def build():\n    a = primitive('cube')\n    paint_faces(a, lambda p: (lambda q: a)(p), '#fff')\n"),
    "predicate_attr": "def build():\n    a = primitive('cube')\n    paint_faces(a, lambda p: p.foo > 1, '#fff')\n",
    "predicate_dunder": "def build():\n    a = primitive('cube')\n    paint_faces(a, lambda p: p.__class__, '#fff')\n",
    "predicate_call": "def build():\n    a = primitive('cube')\n    paint_faces(a, lambda p: open('x'), '#fff')\n",
    "predicate_handle_capture": "def build():\n    a = primitive('cube')\n    paint_faces(a, lambda p: a, '#fff')\n",
    "no_build": "def make():\n    pass\n",
    "build_with_params": "def build(x):\n    pass\n",
    "top_level_call": "print('x')\ndef build():\n    pass\n",
    "top_level_loop": "for i in range(3):\n    pass\ndef build():\n    pass\n",
    "syntax_error": "def build(:\n    pass\n",
    "null_byte": "def build():\n    x = 1\x00\n",
    "unknown_kit": "import evilkit\ndef build():\n    pass\n",
    "from_import_kit": "from propkit import box\ndef build():\n    box('a', (1, 1, 1))\n",
    "from_os_import": "from os import path\ndef build():\n    pass\n",
    "import_star": "from math import *\ndef build():\n    pass\n",
    "relative_import": "from . import sibling\ndef build():\n    pass\n",
    "kwargs_expansion": "def build():\n    primitive('cube', **{'size': 1})\n",
    "star_call": "def build():\n    primitive(*['cube'])\n",
    "math_dunder": "import math\ndef build():\n    x = math.__loader__\n",
    "math_unknown": "import math\ndef build():\n    x = math.system\n",
    "random_unknown": "import random\ndef build():\n    x = random.SystemRandom\n",
    "str_method_unknown": "def build():\n    x = 'a'.encode()\n",
    "mro": "def build():\n    x = [].__mro__\n",
    "subclasses": "def build():\n    x = [].__subclasses__()\n",
}

ALL_EXPLOITS = {}
for _group in (OLD_EXPLOITS, FORMAT_EXPLOITS, NEW_EXPLOITS):
    ALL_EXPLOITS.update(_group)


@pytest.mark.parametrize("name", sorted(ALL_EXPLOITS))
def test_adversarial_script_is_rejected_with_a_line_number(name):
    message = reject(ALL_EXPLOITS[name])
    assert LINE.match(message), message
    assert "internal error" not in message, message


@pytest.mark.parametrize("name", sorted(OLD_EXPLOITS))
def test_old_exploits_without_the_bpy_import_never_escape(name):
    """Dropping the import line must not make a once-dangerous body acceptable or crash the recorder."""
    source = "\n".join(line for line in OLD_EXPLOITS[name].splitlines() if not line.startswith(("import bpy", "from bpy")))
    try:
        result = compile_script(source + "\n", MANIFEST)
    except ScriptError as exc:
        assert "internal error" not in str(exc)
        return
    for op in result.ops:
        assert op["op"].split(".", 1)[0] in ("kit", "core")


def test_adversarial_corpus_is_large_enough():
    assert len(ALL_EXPLOITS) >= 100


def test_win_plus_reg_string_is_just_data():
    result = rec("def build():\n    name = 'win' + 'reg'\n    primitive('cube', name)\n")
    assert result.ops[0]["args"][1] == "winreg"  # a string is data; there is nothing to import it with


# ---------------------------------------------------------------- P1: closed value domain
class _Gen:
    """Random programs over the allowed grammar, including ones that fail at run time."""

    def __init__(self, rng):
        self.rng = rng
        self.names = ["a", "b", "c"]

    def num(self, depth=0):
        r = self.rng
        if depth > 2 or r.random() < 0.4:
            return r.choice(["0", "1", "2", "3", "-1", "0.5", "2.5", "7", "100", "1e3", "n", "x", "len(items)"])
        op = r.choice(["+", "-", "*", "/", "//", "%", "**"])
        return "(%s %s %s)" % (self.num(depth + 1), op, self.num(depth + 1))

    def text(self, depth=0):
        r = self.rng
        if depth > 2 or r.random() < 0.4:
            return r.choice(["'ab'", "'x'", "''", "name", "'#aabbcc'", "str(n)"])
        return r.choice([
            "(%s + %s)" % (self.text(depth + 1), self.text(depth + 1)),
            "(%s * %s)" % (self.text(depth + 1), self.num(depth + 1)),
            "%s.upper()" % self.text(depth + 1),
            "('v%%d' %% %s)" % self.num(depth + 1),
            "f'{%s}'" % self.num(depth + 1),
            "%s.join(['a', 'b'])" % self.text(depth + 1),
        ])

    def seq(self, depth=0):
        r = self.rng
        if depth > 2 or r.random() < 0.4:
            return r.choice(["items", "[1, 2, 3]", "(1, 2)", "[]", "list(range(5))"])
        return r.choice([
            "(%s + %s)" % (self.seq(depth + 1), self.seq(depth + 1)),
            "(%s * %s)" % (self.seq(depth + 1), self.num(depth + 1)),
            "[%s for k in range(%s)]" % (self.num(depth + 1), self.num(depth + 1)),
            "sorted(%s)" % self.seq(depth + 1),
            "%s[%s]" % (self.seq(depth + 1), self.num(depth + 1)),
            "{'k': %s}['k']" % self.seq(depth + 1),
        ])

    def stmt(self, depth=0):
        r = self.rng
        kind = r.randrange(9)
        if kind == 0:
            return "n = %s" % self.num()
        if kind == 1:
            return "name = %s" % self.text()
        if kind == 2:
            return "items = %s" % self.seq()
        if kind == 3:
            return "h = primitive('cube', %s, size=%s, loc=(%s, 0, 0))" % (self.text(), self.num(), self.num())
        if kind == 4:
            return "paint(h, %s)" % self.text()
        if kind == 5:
            return "print(%s, %s)" % (self.num(), self.seq())
        if kind == 6 and depth < 2:
            body = "\n".join("    " + line for line in self.stmt(depth + 1).splitlines())
            return "for q in range(%d):\n%s" % (r.randrange(4), body)
        if kind == 7 and depth < 2:
            body = "\n".join("    " + line for line in self.stmt(depth + 1).splitlines())
            return "if %s < %s:\n%s" % (self.num(), self.num(), body)
        return "x = %s" % self.num()

    def program(self):
        lines = ["import math", "import random", "", "def build():", "    n = 1", "    x = 2", "    name = 'p'",
                 "    items = [1, 2]", "    h = primitive('cube')"]
        for _ in range(self.rng.randrange(1, 9)):
            for line in self.stmt().splitlines():
                lines.append("    " + line)
        return "\n".join(lines) + "\n"


def test_random_programs_never_leave_the_value_domain_or_crash():
    """P1: 2000 random programs; every value the interpreter produces is in the closed domain and only
    ScriptError ever escapes (an 'internal error' message means the interpreter itself broke)."""
    rng = random.Random(20240601)
    accepted = rejected = 0
    for index in range(2000):
        source = _Gen(rng).program()
        seen = []

        def probe(value, seen=seen):
            seen.append(value)
            assert is_domain(value), "value escaped the domain: %r" % type(value)

        try:
            result = compile_script(source, MANIFEST, seed=index, probe=probe)
        except ScriptError as exc:
            rejected += 1
            assert LINE.match(str(exc)) or str(exc), source
            assert "internal error" not in str(exc), "%s\n%s" % (exc, source)
            continue
        accepted += 1
        opschema.validate_ops(json.loads(result.text), MANIFEST.exposed)
    assert accepted > 300 and rejected > 100, (accepted, rejected)


# ---------------------------------------------------------------- P2: no generic dispatch
BANNED_CALLS = {"exec", "eval", "compile", "__import__", "getattr", "setattr", "delattr", "type", "vars",
                "globals", "locals"}
ALLOWED_IMPORTS = {"ast", "math", "random", "json", "time", "__future__"}


def _recorder_modules():
    folder = Path(recorder.__file__).parent
    return [folder / name for name in ("recorder.py", "predicates.py", "manifest.py", "values.py", "errors.py")]


def test_recorder_source_has_no_generic_dispatch_and_few_imports():
    for path in _recorder_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = ALLOWED_IMPORTS | ({"dataclasses"} if path.name == "manifest.py" else set())  # data classes only
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in BANNED_CALLS, "%s calls %s" % (path.name, node.func.id)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ("exec", "eval", "compile", "__import__"), path.name
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] in allowed, "%s imports %s" % (path.name, alias.name)
            if isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    assert (node.module or "").split(".")[0] in allowed, "%s imports %s" % (path.name, node.module)
            if isinstance(node, ast.Name):
                assert node.id not in ("__builtins__", "__import__"), path.name


# ---------------------------------------------------------------- caps
def _capped(source, needle=None, limit=2.0):
    started = time.monotonic()
    message = reject(source)
    elapsed = time.monotonic() - started
    assert elapsed < limit, "%s took %.2fs" % (source[:40], elapsed)
    assert "internal error" not in message
    if needle:
        assert needle in message, message
    return message


def test_infinite_loop_hits_the_step_budget():
    _capped("def build():\n    while True:\n        pass\n", "step", limit=5.0)


def test_memory_bombs_are_refused_before_allocation():
    _capped("def build():\n    x = [0] * 10**9\n")
    _capped("def build():\n    x = 2 ** 10**9\n")
    _capped("def build():\n    x = 'a' * 10**9\n")
    _capped("def build():\n    x = list(range(10**9))\n")
    _capped("def build():\n    x = [0] * 200000\n    for i in range(100):\n        y = [x] * 200000\n")
    _capped("def build():\n    s = 'ab'\n    for i in range(40):\n        s = s + s\n")
    _capped("def build():\n    n = 1\n    for i in range(200):\n        n = n * 1000003\n")


def test_deep_recursion_is_refused():
    _capped("def f(n):\n    return f(n + 1)\ndef build():\n    f(0)\n", "deep")
    deep = "def f(n):\n    if n == 0:\n        return 0\n    return f(n - 1)\ndef build():\n    f(70)\n"
    _capped(deep, "deep")


def test_deeply_nested_source_is_a_script_error_not_a_crash():
    source = "def build():\n    x = " + "(" * 25_000 + "1" + ")" * 25_000 + "\n"
    assert len(source) < recorder.MAX_SOURCE_CHARS
    _capped(source)
    _capped("def build():\n    x = " + "[" * 5_000 + "]" * 5_000 + "\n")


def test_too_many_calls_are_refused():
    body = "    p()\n" * (opschema.MAX_OPS + 1)
    message = _capped("def p():\n    primitive('cube')\n\ndef build():\n" + body, "more than")
    assert str(opschema.MAX_OPS) in message


def test_source_size_cap():
    message = reject("x = 1\n" * 20_000)
    assert "limit" in message


def test_nesting_and_string_caps_apply_to_arguments():
    nested = "[" * 9 + "1" + "]" * 9
    assert "nested" in reject("def build():\n    primitive('cube', name=%s)\n" % nested)
    assert "long" in reject("def build():\n    primitive('cube', name='a' * 300)\n")


# ---------------------------------------------------------------- messages the model sees
def test_wrong_kit_kwarg_lists_the_real_parameters():
    message = reject("import propkit\ndef build():\n    propkit.box('A', (1, 1, 1), colour='#fff')\n")
    assert "colour" in message and "hexcol" in message and "size" in message


def test_too_few_kit_arguments_are_named():
    message = reject("import propkit\ndef build():\n    propkit.box('A')\n")
    assert "size" in message


def test_bbox_style_unpack_gives_the_handle_message():
    message = reject("import propkit\ndef build():\n    a = propkit.box('A', (1, 1, 1))\n    w, d, h = a\n")
    assert "can only be passed to other kit/core functions" in message
    assert "Compute positions from the sizes you pass" in message


def test_handle_attribute_gives_the_handle_message():
    assert "can only be passed to other kit/core functions" in reject(
        "def build():\n    a = primitive('cube')\n    x = a.location.z\n")


def test_import_bpy_hint():
    message = reject("import bpy\ndef build():\n    pass\n")
    assert "bpy is not available" in message
    assert "no bpy" in repair_hint(message, "propkit", frozenset(MANIFEST.exposed))


def test_unknown_kit_attribute_message():
    message = reject("import propkit\ndef build():\n    propkit.fly()\n")
    assert "not available in build scripts" in message


def test_name_error_format_matches_the_repair_hint():
    message = reject("def build():\n    wheel('w', 1)\n")
    assert "NameError: name 'wheel' is not defined" in message
    hint = repair_hint(message, "propkit", frozenset(MANIFEST.exposed))
    assert "propkit.wheel(...)" in hint


def test_runtime_errors_carry_the_line_number():
    assert reject("def build():\n    x = 1\n    y = 1 / 0\n").startswith("line 3")
    assert reject("def build():\n    x = [1]\n\n    y = x[5]\n").startswith("line 4")


def test_no_manifest_means_no_kit_functions():
    message = reject("import propkit\ndef build():\n    propkit.box('A', (1, 1, 1))\n", manifest=None)
    assert LINE.match(message)
