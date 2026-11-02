"""The ``ops.json`` grammar, the core op table and the validator.  Pure stdlib, shipped into Blender.

The recorder validates what it writes and the Blender harness validates what it reads (fail
closed), with this same module.  It must not import anything from the package.
"""
from __future__ import annotations

import math

VERSION = 1
MAX_OPS = 5000
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_STRING = 256
MAX_NESTING = 8
MAX_LEAVES = 2_000_000
MAX_INT = 2 ** 63
MAX_LOG_LINES = 50
MAX_LOG_CHARS = 2000
MAX_PRED_DEPTH = 32
MAX_PRED_NODES = 2000
MAX_PRED_POWER = 64
MAX_PRED_DIGITS = 20
MAX_KEY = 64

# name -> (parameter names, required positional count, accepts extra keyword arguments)
CORE_SIGS = {
    "primitive": (("kind", "name", "size", "radius", "depth", "segments", "rings", "subdivisions",
                   "major_radius", "minor_radius", "loc", "rot", "scale"), 1, False),
    "mesh": (("name", "verts", "faces", "loc", "rot", "scale"), 3, False),
    "transform": (("obj", "loc", "rot", "scale"), 1, False),
    "shade_smooth": (("obj", "smooth", "angle"), 1, False),
    "modifier": (("obj", "type"), 2, True),
    "paint": (("obj", "colour"), 2, False),
    "paint_faces": (("obj", "predicate", "colour"), 3, False),
    "join": (("objs", "name"), 2, False),
    "delete": (("obj",), 1, False),
}
CORE_PREDICATE_PARAMS = {"paint_faces": ("predicate",)}

_FACE_ATTRS = {"center": ("x", "y", "z"), "normal": ("x", "y", "z"), "index": (), "area": (), "material_index": ()}
PRED_MATH = frozenset({"sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sqrt", "radians", "degrees", "floor",
                       "ceil", "hypot", "pow", "exp", "log", "fabs", "copysign", "fmod", "isclose"})
PRED_BUILTINS = frozenset({"abs", "min", "max", "round"})
_BINARY = frozenset({"+", "-", "*", "/", "//", "%", "**"})
_COMPARE = frozenset({"<", "<=", ">", ">=", "==", "!="})


class OpsError(ValueError):
    """The document is not a valid op list."""


def _fail(where, message):
    raise OpsError("%s: %s" % (where, message))


def validate_pred(tree, where="predicate"):
    """Check a predicate tree: known heads, arities, depth <= 32, nodes <= 2000."""
    count = [0]

    def node(item, depth):
        count[0] += 1
        if count[0] > MAX_PRED_NODES:
            _fail(where, "more than %d nodes" % MAX_PRED_NODES)
        if depth > MAX_PRED_DEPTH:
            _fail(where, "nested deeper than %d" % MAX_PRED_DEPTH)
        if item is None:
            _fail(where, "null is not a predicate value")
        if isinstance(item, bool):
            return
        if isinstance(item, int):
            if not -MAX_INT < item < MAX_INT:
                _fail(where, "integer out of range")
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                _fail(where, "non-finite number")
            return
        if not isinstance(item, list) or not item or not isinstance(item[0], str):
            _fail(where, "malformed node")
        head, rest = item[0], item[1:]
        if head == "s":
            _fail(where, "strings are not allowed in a predicate (predicates are numbers and bools)")
        elif head == "f":
            if not rest or rest[0] not in _FACE_ATTRS:
                _fail(where, "unknown face attribute")
            axes = _FACE_ATTRS[rest[0]]
            if len(rest) != (2 if axes else 1) or (axes and rest[1] not in axes):
                _fail(where, "malformed face attribute")
        elif head in _BINARY or head in _COMPARE:
            if len(rest) != 2:
                _fail(where, "%s takes two operands" % head)
            if head == "**" and not (isinstance(rest[1], (int, float)) and not isinstance(rest[1], bool)
                                     and abs(rest[1]) <= MAX_PRED_POWER):
                _fail(where, "the exponent of ** must be a number literal between -%d and %d" % (MAX_PRED_POWER, MAX_PRED_POWER))
            for sub in rest:
                node(sub, depth + 1)
        elif head in ("neg", "pos", "not"):
            if len(rest) != 1:
                _fail(where, "%s takes one operand" % head)
            node(rest[0], depth + 1)
        elif head in ("and", "or"):
            if len(rest) < 2:
                _fail(where, "%s takes at least two operands" % head)
            for sub in rest:
                node(sub, depth + 1)
        elif head == "if":
            if len(rest) != 3:
                _fail(where, "if takes three operands")
            for sub in rest:
                node(sub, depth + 1)
        elif head == "call":
            if not rest or not isinstance(rest[0], str):
                _fail(where, "malformed call")
            name = rest[0]
            if name.startswith("math."):
                if name[5:] not in PRED_MATH:
                    _fail(where, "unknown function")
            elif name not in PRED_BUILTINS:
                _fail(where, "unknown function")
            if len(rest) < 2 or len(rest) > 4:
                _fail(where, "bad argument count")
            if name == "abs" and len(rest) != 2:
                _fail(where, "abs takes one argument")
            if name == "round":
                if len(rest) > 3:
                    _fail(where, "round takes a number and optional digits")
                if len(rest) == 3 and not (isinstance(rest[2], int) and not isinstance(rest[2], bool)
                                           and abs(rest[2]) <= MAX_PRED_DIGITS):
                    _fail(where, "round digits must be an integer literal between -%d and %d"
                          % (MAX_PRED_DIGITS, MAX_PRED_DIGITS))
            for sub in rest[1:]:
                node(sub, depth + 1)
        else:
            _fail(where, "unknown node")

    node(tree, 0)


def _value(item, depth, ctx, where):
    """One argument value: null, bool, finite number, short string, array, ``$ref`` or ``$pred`` object."""
    if item is None or isinstance(item, bool):
        return
    if isinstance(item, int):
        ctx["leaves"] += 1
        if not -MAX_INT < item < MAX_INT:
            _fail(where, "integer out of range")
    elif isinstance(item, float):
        ctx["leaves"] += 1
        if not math.isfinite(item):
            _fail(where, "non-finite number")
    elif isinstance(item, str):
        ctx["leaves"] += 1
        if len(item) > MAX_STRING:
            _fail(where, "string longer than %d characters" % MAX_STRING)
    elif isinstance(item, list):
        if depth > MAX_NESTING:
            _fail(where, "nested deeper than %d" % MAX_NESTING)
        for sub in item:
            _value(sub, depth + 1, ctx, where)
    elif isinstance(item, dict):
        if len(item) != 1:
            _fail(where, "an object must have exactly one key ($ref or $pred)")
        key = next(iter(item))
        if key == "$ref":
            ref = item[key]
            if isinstance(ref, bool) or not isinstance(ref, int) or ref < 1 or ref >= ctx["id"]:
                _fail(where, "$ref must name an earlier op")
            ctx["leaves"] += 1
        elif key == "$pred":
            validate_pred(item[key], where)
            ctx["leaves"] += 1
        else:
            _fail(where, "unknown object key")
    else:
        _fail(where, "unsupported value")
    if ctx["leaves"] > MAX_LEAVES:
        _fail(where, "more than %d values" % MAX_LEAVES)


def validate_ops(doc, kit_names=frozenset()):
    """Validate a whole document; raises ``OpsError``.  ``kit_names`` are the kit functions that may be called."""
    if not isinstance(doc, dict) or set(doc) != {"version", "kit", "seed", "ops", "log"}:
        _fail("ops.json", "unexpected top-level keys")
    if isinstance(doc["version"], bool) or doc["version"] != VERSION:
        _fail("ops.json", "unsupported version")
    if not isinstance(doc["kit"], str) or len(doc["kit"]) > MAX_KEY:
        _fail("ops.json", "bad kit name")
    if isinstance(doc["seed"], bool) or not isinstance(doc["seed"], int):
        _fail("ops.json", "bad seed")
    log = doc["log"]
    if not isinstance(log, list) or len(log) > MAX_LOG_LINES or not all(
            isinstance(line, str) and len(line) <= MAX_LOG_CHARS for line in log):
        _fail("ops.json", "bad log")
    ops = doc["ops"]
    if not isinstance(ops, list) or len(ops) > MAX_OPS:
        _fail("ops.json", "more than %d ops" % MAX_OPS)
    allowed_kit = frozenset(kit_names)
    ctx = {"leaves": 0, "id": 0}
    for index, op in enumerate(ops, start=1):
        where = "op %d" % index
        if not isinstance(op, dict) or set(op) != {"id", "line", "op", "args", "kwargs"}:
            _fail(where, "unexpected keys")
        if isinstance(op["id"], bool) or op["id"] != index:
            _fail(where, "ids must be 1, 2, 3 ... in order")
        if isinstance(op["line"], bool) or not isinstance(op["line"], int) or op["line"] < 0:
            _fail(where, "bad line")
        name = op["op"]
        if not isinstance(name, str):
            _fail(where, "bad op name")
        kind, _, short = name.partition(".")
        if not ((kind == "core" and short in CORE_SIGS) or (kind == "kit" and short in allowed_kit)):
            _fail(where, "unknown op %r" % name[:40])
        if not isinstance(op["args"], list) or not isinstance(op["kwargs"], dict):
            _fail(where, "args must be an array and kwargs an object")
        ctx["id"] = index
        for item in op["args"]:
            _value(item, 1, ctx, where)
        for key, item in op["kwargs"].items():
            if not isinstance(key, str) or not key.isidentifier() or len(key) > MAX_KEY or key.startswith("__"):
                _fail(where, "bad keyword name")
            _value(item, 1, ctx, where)


__all__ = ["CORE_PREDICATE_PARAMS", "CORE_SIGS", "MAX_JSON_BYTES", "MAX_OPS", "OpsError", "PRED_BUILTINS", "PRED_MATH",
           "validate_ops", "validate_pred"]
