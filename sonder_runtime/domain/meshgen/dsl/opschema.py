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
MAX_COORD = 100000.0
MAX_VERTS = 200000
MAX_FACES = 400000
PRIMITIVES = ("cube", "uv_sphere", "ico_sphere", "cylinder", "cone", "torus", "plane", "circle", "grid")

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

# core op -> parameter -> (kind, low, high, may be null).  The recorder checks these when a script is recorded and
# the Blender harness checks them again; replay takes its ranges from here, so the two cannot drift.
#   enum(values)  num(low, high)  int(low, high)  vec3(low, high): a number or three numbers  vec3x3: exactly three
#   text  bool  obj: a result handle  objs: a list of handles  pred  colour: a string  verts  faces  modtype: a string
_POS = (0.0001, MAX_COORD)
CORE_PARAM_KINDS = {
    "primitive": {
        "kind": ("enum", PRIMITIVES, None, False), "name": ("text", None, None, True),
        "size": ("num",) + _POS + (False,), "radius": ("num",) + _POS + (False,), "depth": ("num",) + _POS + (False,),
        "segments": ("int", 3, 256, False), "rings": ("int", 2, 128, False), "subdivisions": ("int", 1, 5, False),
        "major_radius": ("num",) + _POS + (False,), "minor_radius": ("num",) + _POS + (False,),
        "loc": ("vec3", -MAX_COORD, MAX_COORD, True), "rot": ("vec3", -3600.0, 3600.0, True),
        "scale": ("vec3", -MAX_COORD, MAX_COORD, True),
    },
    "mesh": {
        "name": ("text", None, None, True), "verts": ("verts", None, None, False), "faces": ("faces", None, None, False),
        "loc": ("vec3", -MAX_COORD, MAX_COORD, True), "rot": ("vec3", -3600.0, 3600.0, True),
        "scale": ("vec3", -MAX_COORD, MAX_COORD, True),
    },
    "transform": {
        "obj": ("obj", None, None, False), "loc": ("vec3", -MAX_COORD, MAX_COORD, True),
        "rot": ("vec3", -3600.0, 3600.0, True), "scale": ("vec3", -MAX_COORD, MAX_COORD, True),
    },
    "shade_smooth": {"obj": ("obj", None, None, False), "smooth": ("bool", None, None, False),
                     "angle": ("num", 0.0, 180.0, True)},
    "modifier": {"obj": ("obj", None, None, False), "type": ("modtype", None, None, False)},
    "paint": {"obj": ("obj", None, None, False), "colour": ("colour", None, None, False)},
    "paint_faces": {"obj": ("obj", None, None, False), "predicate": ("pred", None, None, False),
                    "colour": ("colour", None, None, False)},
    "join": {"objs": ("objs", None, None, False), "name": ("text", None, None, True)},
    "delete": {"obj": ("obj", None, None, False)},
}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _kind_of(value):
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "a true/false value"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a result or predicate"
    return "this kind of value"


def check_core_arg(op, param, value):
    """Check one argument of a core op (``op`` is ``primitive``, ``mesh`` ...; ``value`` is the JSON form, so a
    result is ``{"$ref": n}``).  Raises ``OpsError`` naming the op and parameter.  Parameters that are not in the
    table (the free properties of ``modifier``) are not checked here."""
    spec = CORE_PARAM_KINDS.get(op, {}).get(param)
    if spec is None:
        return
    kind, low, high, nullable = spec
    label = "%s %s" % (op, param)
    if value is None:
        if nullable:
            return
        raise OpsError("%s is required (not None)" % label)
    if kind == "enum":
        if not isinstance(value, str) or value not in low:
            raise OpsError("%s must be one of: %s" % (label, ", ".join(low)))
    elif kind == "num":
        if not _is_number(value) or not low <= value <= high:
            hint = " (ONE number; for a box use scale=(x, y, z))" if (op, param) == ("primitive", "size") else ""
            raise OpsError("%s must be one number between %g and %g, not %s%s" % (label, low, high, _kind_of(value), hint))
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise OpsError("%s must be a whole number between %d and %d, not %s" % (label, low, high, _kind_of(value)))
    elif kind == "vec3":
        items = [value, value, value] if _is_number(value) else value
        if not isinstance(items, list) or len(items) != 3 or not all(_is_number(i) and low <= i <= high for i in items):
            raise OpsError("%s must be one number or three numbers between %g and %g, not %s"
                           % (label, low, high, _kind_of(value)))
        if param == "rot" and _is_number(value):
            raise OpsError("%s must be three numbers (degrees), for example rot=(0, 90, 0)" % label)
    elif kind == "text" or kind == "modtype" or kind == "colour":
        if not isinstance(value, str):
            raise OpsError("%s must be a string, not %s" % (label, _kind_of(value)))
    elif kind == "bool":
        if not isinstance(value, bool):
            raise OpsError("%s must be true or false, not %s" % (label, _kind_of(value)))
    elif kind == "obj":
        if not (isinstance(value, dict) and set(value) == {"$ref"}):
            raise OpsError("%s must be the result of an earlier kit/core call, not %s" % (label, _kind_of(value)))
    elif kind == "objs":
        if not (isinstance(value, list) and 1 <= len(value) <= MAX_OPS and all(
                isinstance(item, dict) and set(item) == {"$ref"} for item in value)):
            raise OpsError("%s must be a non-empty list of results of earlier kit/core calls" % label)
    elif kind == "pred":
        if not (isinstance(value, dict) and set(value) == {"$pred"}):
            raise OpsError("%s must be an inline lambda" % label)
    elif kind == "verts":
        if not (isinstance(value, list) and 1 <= len(value) <= MAX_VERTS):
            raise OpsError("%s must be a list of 1 to %d points" % (label, MAX_VERTS))
        for point in value:
            if not (isinstance(point, list) and len(point) == 3 and all(
                    _is_number(i) and -MAX_COORD <= i <= MAX_COORD for i in point)):
                raise OpsError("%s: every vertex must be three numbers (x, y, z), not %s"
                               % (label, _kind_of(point) if not isinstance(point, list) else "%d values" % len(point)))
    elif kind == "faces":
        if not (isinstance(value, list) and 1 <= len(value) <= MAX_FACES):
            raise OpsError("%s must be a list of 1 to %d faces" % (label, MAX_FACES))
        for face in value:
            if not (isinstance(face, list) and 3 <= len(face) <= 8 and all(
                    isinstance(i, int) and not isinstance(i, bool) and i >= 0 for i in face)):
                raise OpsError("%s: each face needs 3 to 8 vertex indices (whole numbers, 0 or more)" % label)
            if len(set(face)) != len(face):
                raise OpsError("%s: a face may not repeat a vertex index" % label)


def check_core_call(op, args, kwargs):
    """Check every argument of one core op call (JSON form); ``mesh`` also checks that face indices exist."""
    names = CORE_SIGS[op][0]
    given = {}
    for position, value in enumerate(args):
        if position < len(names):
            given[names[position]] = value
    given.update(kwargs)
    for param, value in given.items():
        check_core_arg(op, param, value)
    if op == "mesh" and isinstance(given.get("verts"), list) and isinstance(given.get("faces"), list):
        count = len(given["verts"])
        for face in given["faces"]:
            if max(face) >= count:
                raise OpsError("mesh faces: vertex index %d does not exist (there are %d vertices)" % (max(face), count))


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
        if kind == "core":
            try:
                check_core_call(short, op["args"], op["kwargs"])
            except OpsError as exc:
                _fail(where, str(exc))


__all__ = ["CORE_PARAM_KINDS", "CORE_PREDICATE_PARAMS", "CORE_SIGS", "check_core_arg", "check_core_call", "MAX_JSON_BYTES", "MAX_OPS", "OpsError", "PRED_BUILTINS", "PRED_MATH",
           "validate_ops", "validate_pred"]
