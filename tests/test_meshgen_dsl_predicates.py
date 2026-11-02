"""Face predicates: lambdas serialised to a JSON tree, never executed."""
from __future__ import annotations

import json

import pytest

from sonder_runtime.domain.meshgen.dsl import opschema
from sonder_runtime.domain.meshgen.dsl.errors import ScriptError
from sonder_runtime.domain.meshgen.dsl.recorder import compile_script


def pred_of(expr, prelude=""):
    source = "import math\ndef build():\n%s    a = primitive('cube')\n    paint_faces(a, lambda p: %s, '#ff0000')\n" % (
        prelude, expr)
    result = compile_script(source)
    return result.ops[-1]["args"][1]["$pred"]


def test_simple_comparison_round_trips_through_json():
    tree = pred_of("p.center.z > 0.5")
    assert json.loads(json.dumps(tree)) == tree
    opschema.validate_pred(tree)


def test_free_variables_are_captured_by_value():
    tree = pred_of("p.center.z > limit", prelude="    limit = 0.75\n")
    flat = json.dumps(tree)
    assert "0.75" in flat and "limit" not in flat
    # later changes to the variable do not alter the recorded tree
    source = ("def build():\n    limit = 0.75\n    a = primitive('cube')\n"
              "    paint_faces(a, lambda p: p.center.z > limit, '#fff')\n    limit = 9\n")
    assert "0.75" in json.dumps(compile_script(source).ops[-1]["args"][1])


def test_string_and_bool_captures():
    assert "wood" in json.dumps(pred_of("p.material_index == 0 or name == 'wood'", prelude="    name = 'wood'\n"))
    tree = pred_of("flag and p.area > 0.1", prelude="    flag = True\n")
    assert isinstance(tree, list)


def test_boolean_logic_and_arithmetic_and_calls():
    for expr in ("p.center.z > 0.5 and p.normal.z > 0.9", "not (p.index < 2)", "abs(p.center.x) < 0.3",
                 "min(p.center.x, p.center.y) > -1", "math.sqrt(p.area) > 0.1", "p.center.z * 2 + 1 > 3",
                 "p.center[2] > 0.5", "(p.center.x if p.index > 1 else 0) > 0", "0 < p.center.z < 2",
                 "p.index % 2 == 0", "round(p.area, 2) > 0"):
        tree = pred_of(expr)
        opschema.validate_pred(tree)
        assert json.loads(json.dumps(tree)) == tree, expr


@pytest.mark.parametrize("expr", [
    "p.foo > 1", "p.center.w > 1", "p.center.__class__", "p.__dict__", "q > 1", "open('x')", "__import__('os')",
    "[x for x in p.center]", "p.center.z.real", "p.index.bit_length() > 1", "math.system('x')", "str(p.index) == '1'",
    "(lambda q: q)(1)", "p.center[3]", "p.center['z']", "p.normal.x @ 2", "1 << p.index",
])
def test_unsupported_predicate_expressions_are_rejected(expr):
    source = "import math\ndef build():\n    a = primitive('cube')\n    paint_faces(a, lambda p: %s, '#fff')\n" % expr
    with pytest.raises(ScriptError) as info:
        compile_script(source)
    assert str(info.value).startswith("line 4"), str(info.value)


def test_non_numeric_captures_are_rejected():
    for value in ("[1, 2]", "primitive('cube')", "{'a': 1}", "None"):
        source = "def build():\n    v = %s\n    a = primitive('cube')\n    paint_faces(a, lambda p: p.center.z > v, '#fff')\n" % value
        with pytest.raises(ScriptError):
            compile_script(source)


def test_two_parameter_and_default_lambdas_are_rejected():
    for lam in ("lambda p, q: p.center.z > 0", "lambda: 1", "lambda p=1: p.center.z", "lambda *p: 1", "lambda **k: 1"):
        source = "def build():\n    a = primitive('cube')\n    paint_faces(a, %s, '#fff')\n" % lam
        with pytest.raises(ScriptError):
            compile_script(source)


def test_predicate_must_be_inline_lambda():
    for arg in ("None", "'abs'", "primitive", "5"):
        source = "def build():\n    a = primitive('cube')\n    paint_faces(a, %s, '#fff')\n" % arg
        with pytest.raises(ScriptError):
            compile_script(source)


def test_lambda_outside_a_predicate_position_is_rejected():
    with pytest.raises(ScriptError):
        compile_script("def build():\n    f = lambda p: p.center.z\n")
    with pytest.raises(ScriptError):
        compile_script("def build():\n    a = primitive('cube')\n    paint(a, lambda p: 1)\n")


def test_predicate_size_limits():
    deep = " and ".join(["p.center.z > %d" % i for i in range(opschema.MAX_PRED_NODES)])
    source = "def build():\n    a = primitive('cube')\n    paint_faces(a, lambda p: %s, '#fff')\n" % deep
    with pytest.raises(ScriptError):
        compile_script(source)
    nested = "p.center.z"
    for _ in range(opschema.MAX_PRED_DEPTH + 2):
        nested = "(%s + 1)" % nested
    with pytest.raises(ScriptError):
        compile_script("def build():\n    a = primitive('cube')\n    paint_faces(a, lambda p: %s > 0, '#fff')\n" % nested)


def test_validate_pred_rejects_hand_crafted_trees():
    for bad in (["call", "system", ["s", "x"]], ["f", "__class__"], ["unknown"], {"a": 1}, ["f", "center", "w"],
                ["call", "math.system"], ["+", 1], ["s", "x" * 300]):
        with pytest.raises(opschema.OpsError):
            opschema.validate_pred(bad)
