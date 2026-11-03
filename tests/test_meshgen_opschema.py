"""ops.json grammar: the same validator guards the recorder's output and the harness' input."""
from __future__ import annotations

import copy
import json

import pytest

from sonder_runtime.domain.meshgen.dsl import opschema
from sonder_runtime.domain.meshgen.dsl.opschema import OpsError, validate_ops

KIT = frozenset({"box", "wheel"})


def doc(*ops):
    full = []
    for index, (name, args, kwargs) in enumerate(ops, start=1):
        full.append({"id": index, "line": index, "op": name, "args": args, "kwargs": kwargs})
    return {"version": 1, "kit": "propkit", "seed": 0, "ops": full, "log": []}


def bad(document, needle=None):
    with pytest.raises(OpsError) as info:
        validate_ops(document, KIT)
    if needle:
        assert needle in str(info.value), str(info.value)


GOOD = doc(("kit.box", ["A", [1, 1, 1]], {"radius": 0.1}), ("core.paint", [{"$ref": 1}, "#ffffff"], {}),
           ("core.join", [[{"$ref": 1}, {"$ref": 2}], "J"], {}))


def test_a_well_formed_document_is_accepted():
    validate_ops(copy.deepcopy(GOOD), KIT)
    validate_ops(json.loads(json.dumps(GOOD)), KIT)
    validate_ops(doc(), KIT)


def test_unknown_top_level_and_op_keys():
    extra = copy.deepcopy(GOOD)
    extra["extra"] = 1
    bad(extra, "top-level")
    missing = copy.deepcopy(GOOD)
    del missing["log"]
    bad(missing)
    odd = copy.deepcopy(GOOD)
    odd["ops"][0]["code"] = "x"
    bad(odd, "keys")
    for value in ([], "x", 3, None):
        bad(value)


def test_unknown_and_forbidden_op_names():
    for name in ("winreg.OpenKey", "kit.finalize_and_export", "core.__import__", "kit.__class__", "core.", "core",
                 "kit.os", "", "core.eval", "Core.paint", "kit.PALETTE.x"):
        bad(doc((name, [], {})), "unknown op")
    bad(doc((5, [], {})))


def test_ids_must_be_sequential_and_refs_must_point_backwards():
    wrong = copy.deepcopy(GOOD)
    wrong["ops"][1]["id"] = 5
    bad(wrong, "ids")
    bad(doc(("core.paint", [{"$ref": 1}, "#fff"], {})), "$ref")  # self reference
    bad(doc(("core.paint", [{"$ref": 2}, "#fff"], {}), ("core.delete", [{"$ref": 1}], {})), "$ref")  # forward
    bad(doc(("core.paint", [{"$ref": 9}, "#fff"], {})), "$ref")  # undefined
    bad(doc(("core.primitive", ["cube"], {}), ("core.delete", [{"$ref": 0}], {})), "$ref")
    bad(doc(("core.primitive", ["cube"], {}), ("core.delete", [{"$ref": True}], {})), "$ref")
    bad(doc(("core.primitive", ["cube"], {}), ("core.delete", [{"$ref": "1"}], {})), "$ref")
    bad(doc(("core.primitive", ["cube"], {}), ("core.delete", [{"$ref": 1.0}], {})), "$ref")


def test_object_values_have_exactly_one_known_key():
    first = ("core.primitive", ["cube"], {})
    bad(doc(first, ("core.delete", [{"$ref": 1, "x": 2}], {})), "exactly one")
    bad(doc(first, ("core.delete", [{"$code": 1}], {})), "unknown object key")
    bad(doc(first, ("core.delete", [{}], {})), "exactly one")
    bad(doc(first, ("core.delete", [{"$pred": ["unknown"]}], {})), "unknown node")


def test_numbers_and_strings_are_bounded():
    for number in (float("nan"), float("inf"), -float("inf"), 2 ** 63, -(2 ** 63)):
        bad(doc(("kit.box", ["cube"], {"size": number})))
    bad(doc(("kit.box", ["x" * 300], {})), "string")
    validate_ops(doc(("kit.box", ["x" * opschema.MAX_STRING], {})), KIT)
    bad(doc(("kit.box", [object()], {})), "unsupported")


def test_nesting_depth_limit():
    ok = 1
    for _ in range(opschema.MAX_NESTING):
        ok = [ok]
    validate_ops(doc(("kit.box", [ok], {})), KIT)
    deeper = [ok]
    bad(doc(("kit.box", [deeper], {})), "nested")


def test_leaf_count_limit():
    chunk = [0] * 1000
    args = [chunk] * (opschema.MAX_LEAVES // 1000 + 1)
    bad(doc(("kit.box", [args], {})), "more than")


def test_op_count_and_log_limits():
    many = doc()
    many["ops"] = [{"id": i, "line": 1, "op": "core.primitive", "args": [], "kwargs": {}}
                   for i in range(1, opschema.MAX_OPS + 2)]
    bad(many, "more than")
    long_log = doc()
    long_log["log"] = ["x"] * (opschema.MAX_LOG_LINES + 1)
    bad(long_log, "log")
    long_line = doc()
    long_line["log"] = ["x" * (opschema.MAX_LOG_CHARS + 1)]
    bad(long_line, "log")
    not_text = doc()
    not_text["log"] = [1]
    bad(not_text, "log")


def test_keyword_names_are_identifiers():
    for key in ("__class__x y", "a.b", "", "1a", "a" * 100):
        bad(doc(("core.primitive", [], {key: 1})), "keyword")


def test_args_and_kwargs_types():
    bad(doc(("core.primitive", {}, {})), "array")
    first = doc(("core.primitive", [], {}))
    first["ops"][0]["kwargs"] = []
    bad(first, "array")


def test_version_and_seed():
    old = copy.deepcopy(GOOD)
    old["version"] = 2
    bad(old, "version")
    flagged = copy.deepcopy(GOOD)
    flagged["version"] = True
    bad(flagged, "version")
    seeded = copy.deepcopy(GOOD)
    seeded["seed"] = "1"
    bad(seeded, "seed")
    kit = copy.deepcopy(GOOD)
    kit["kit"] = 5
    bad(kit, "kit")


def test_core_signature_table_matches_the_digest():
    from sonder_runtime.domain.meshgen.dsl.core_api import core_api_digest

    text = core_api_digest()
    for name in opschema.CORE_SIGS:
        assert "%s(" % name in text


# --- core argument kinds: the host validator refuses what replay would refuse ---

BAD_CORE_ARGS = [
    ("primitive", ["plane"], {"size": [0.8, 0.4]}),
    ("primitive", ["cube"], {"size": "big"}),
    ("primitive", ["cube"], {"size": 0}),
    ("primitive", ["uv_sphere"], {"segments": 8.5}),
    ("primitive", ["uv_sphere"], {"segments": 2}),
    ("primitive", ["blob"], {}),
    ("primitive", ["cube"], {"rot": 90}),
    ("primitive", ["cube"], {"loc": [1, 2]}),
    ("primitive", ["cube"], {"scale": [1, 2, 3, 4]}),
    ("mesh", ["M", [[0, 0], [1, 0], [0, 1]], [[0, 1, 2]]], {}),
    ("mesh", ["M", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1]]], {}),
    ("mesh", ["M", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 5]]], {}),
    ("mesh", ["M", [[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 0, 1]]], {}),
    ("shade_smooth", [{"$ref": 1}, "yes"], {}),
    ("paint", [{"$ref": 1}, 5], {}),
]


@pytest.mark.parametrize("op,args,kwargs", BAD_CORE_ARGS)
def test_validate_ops_refuses_badly_typed_core_arguments(op, args, kwargs):
    prefix = [] if op in ("primitive", "mesh") else [("core.primitive", ["cube"], {})]
    bad(doc(*prefix, ("core." + op, args, kwargs)))


def test_check_core_arg_accepts_in_range_values_and_names_the_parameter():
    opschema.check_core_arg("primitive", "size", 1.5)
    opschema.check_core_arg("primitive", "scale", [1, 2, 3])
    opschema.check_core_arg("primitive", "scale", 2)
    with pytest.raises(OpsError) as info:
        opschema.check_core_arg("primitive", "segments", 8.5)
    assert "segments" in str(info.value)


def test_every_core_sig_parameter_has_a_kind():
    for op, (names, _required, extra) in opschema.CORE_SIGS.items():
        for name in names:
            assert name in opschema.CORE_PARAM_KINDS[op] or (extra and name == "type"), (op, name)


def test_detached_is_a_core_op_that_takes_one_handle():
    assert opschema.CORE_SIGS["detached"] == (("obj",), 1, False)
    made = ("core.primitive", ["cube"], {})
    validate_ops(doc(made, ("core.detached", [{"$ref": 1}], {})), KIT)
    validate_ops(doc(made, ("core.detached", [], {"obj": {"$ref": 1}})), KIT)


def test_detached_refuses_anything_that_is_not_a_reference_to_an_earlier_op():
    made = ("core.primitive", ["cube"], {})
    for value in (5, 1.5, "cube", None, True, [1], [{"$ref": 1}], {"$pred": ["f", "index"]}):
        bad(doc(made, ("core.detached", [value], {})), "earlier call")
    bad(doc(made, ("core.detached", [], {"obj": 3})), "earlier call")
    bad(doc(made, ("core.detached", [], {})), "earlier call")
    bad(doc(("core.detached", [{"$ref": 1}], {})), "$ref")           # a reference to itself is not an earlier op
    bad(doc(made, ("core.detached", [{"$ref": 2}], {})), "$ref")
