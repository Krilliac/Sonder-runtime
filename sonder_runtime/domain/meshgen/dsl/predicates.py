"""Serialise a ``lambda p: ...`` in a predicate position into a JSON tree (no code crosses to Blender).

Grammar: numbers and bools (no strings); ``p.center.{x,y,z}``, ``p.normal.{x,y,z}``, ``p.center[i]``,
``p.normal[i]``, ``p.index``, ``p.area``, ``p.material_index``; arithmetic, chained comparisons,
and/or/not, ``a if c else b``; ``abs``/``min``/``max``/``round`` and ``math.*``.  Free variables are
captured BY VALUE and must be numbers or bools.  Evaluated in Blender by ``replay.py`` with a
small recursive evaluator.
"""
from __future__ import annotations

import ast

from .errors import ScriptError
from .opschema import MAX_PRED_DEPTH, MAX_PRED_NODES, PRED_BUILTINS, PRED_MATH, OpsError, validate_pred

_BINOPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**"}
_CMPOPS = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}
_AXES = {"x": 0, "y": 1, "z": 2}
_VECTORS = ("center", "normal")
_SCALARS = ("index", "area", "material_index")


def _lineno(node, default):
    """The source line of an AST node, or ``default`` for nodes that carry none."""
    try:
        return node.lineno
    except AttributeError:
        return default


def compile_predicate(node, capture, math_aliases=("math",)):
    """Translate ``node`` (an ``ast.Lambda``) to a predicate tree.

    ``capture(name, line)`` returns the current value of a free variable (raising ``ScriptError`` when it is
    unknown).  ``math_aliases`` are the names that refer to the math module in the script.
    """
    line = _lineno(node, 0)
    if not isinstance(node, ast.Lambda):
        raise ScriptError(line, "a predicate must be written as a lambda, for example lambda p: p.center.z > 0.5")
    args = node.args
    if (len(args.args) != 1 or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or args.defaults):
        raise ScriptError(line, "a predicate lambda takes exactly one parameter (the face)")
    param = args.args[0].arg
    nodes = [0]

    def err(sub, message):
        return ScriptError(_lineno(sub, line), message)

    def face(sub):
        """``p.attr`` / ``p.vec.axis`` / ``p.vec[i]`` -> ["f", ...] or None when ``sub`` is not a face path."""
        if isinstance(sub, ast.Attribute):
            base = sub.value
            if isinstance(base, ast.Name) and base.id == param:
                if sub.attr in _SCALARS:
                    return ["f", sub.attr]
                if sub.attr in _VECTORS:
                    raise err(sub, "use p.%s.x, p.%s.y or p.%s.z (or p.%s[0..2])" % ((sub.attr,) * 4))
                raise err(sub, "the face has no attribute %r; use center, normal, index, area or material_index"
                          % sub.attr[:30])
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) and base.value.id == param:
                if base.attr in _VECTORS and sub.attr in _AXES:
                    return ["f", base.attr, sub.attr]
                raise err(sub, "unsupported face attribute; use center/normal with x, y or z")
        if isinstance(sub, ast.Subscript):
            base = sub.value
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) and base.value.id == param \
                    and base.attr in _VECTORS:
                index = sub.slice
                if isinstance(index, ast.Constant) and isinstance(index.value, int) and not isinstance(index.value, bool) \
                        and 0 <= index.value <= 2:
                    return ["f", base.attr, "xyz"[index.value]]
                raise err(sub, "index p.%s with 0, 1 or 2" % base.attr)
        return None

    def walk(sub, depth):
        nodes[0] += 1
        if nodes[0] > MAX_PRED_NODES or depth > MAX_PRED_DEPTH:
            raise err(sub, "the predicate is too large (limit: %d nodes, depth %d)" % (MAX_PRED_NODES, MAX_PRED_DEPTH))
        path = face(sub)
        if path is not None:
            return path
        if isinstance(sub, ast.Constant):
            value = sub.value
            if isinstance(value, bool) or isinstance(value, (int, float)):
                return value
            raise err(sub, "only numbers and bools can appear in a predicate (strings are not allowed)")
        if isinstance(sub, ast.Name):
            if sub.id == param:
                raise err(sub, "use a face attribute such as %s.center.z, not the bare parameter" % param)
            value = capture(sub.id, _lineno(sub, line))
            if isinstance(value, bool) or isinstance(value, (int, float)):
                return value
            raise err(sub, "%r is captured by value in a predicate and must be a number or bool" % sub.id[:40])
        if isinstance(sub, ast.BinOp) and sub.op.__class__ in _BINOPS:
            return [_BINOPS[sub.op.__class__], walk(sub.left, depth + 1), walk(sub.right, depth + 1)]
        if isinstance(sub, ast.UnaryOp):
            if isinstance(sub.op, ast.USub):
                return ["neg", walk(sub.operand, depth + 1)]
            if isinstance(sub.op, ast.UAdd):
                return ["pos", walk(sub.operand, depth + 1)]
            if isinstance(sub.op, ast.Not):
                return ["not", walk(sub.operand, depth + 1)]
        if isinstance(sub, ast.BoolOp):
            head = "and" if isinstance(sub.op, ast.And) else "or"
            return [head] + [walk(value, depth + 1) for value in sub.values]
        if isinstance(sub, ast.Compare):
            if any(op.__class__ not in _CMPOPS for op in sub.ops):
                raise err(sub, "only < <= > >= == != comparisons are supported in a predicate")
            operands = [sub.left] + list(sub.comparators)
            terms = [[_CMPOPS[op.__class__], walk(operands[i], depth + 1), walk(operands[i + 1], depth + 1)]
                     for i, op in enumerate(sub.ops)]
            return terms[0] if len(terms) == 1 else ["and"] + terms
        if isinstance(sub, ast.IfExp):
            return ["if", walk(sub.test, depth + 1), walk(sub.body, depth + 1), walk(sub.orelse, depth + 1)]
        if isinstance(sub, ast.Call) and not sub.keywords:
            func = sub.func
            name = None
            if isinstance(func, ast.Name) and func.id in PRED_BUILTINS:
                name = func.id
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and func.value.id in math_aliases and func.attr in PRED_MATH:
                name = "math." + func.attr
            if name is not None and 1 <= len(sub.args) <= 3 and not any(isinstance(a, ast.Starred) for a in sub.args):
                return ["call", name] + [walk(arg, depth + 1) for arg in sub.args]
            raise err(sub, "only abs, min, max, round and math functions can be called in a predicate")
        raise err(sub, "%s is not supported in a predicate" % sub.__class__.__name__)

    tree = walk(node.body, 0)
    try:
        validate_pred(tree, "line %d" % line)
    except OpsError as exc:
        raise ScriptError(line, "invalid predicate (%s)" % exc) from None
    return tree


__all__ = ["compile_predicate"]
