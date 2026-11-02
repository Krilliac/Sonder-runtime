"""The recorder: an allow-list interpreter for build scripts that only RECORDS kit and core calls.

Model-written code is never executed by CPython.  This module parses it with ``ast`` and evaluates a
small, fixed subset itself; every kit or core call becomes an entry in a JSON op list that trusted code
replays inside sandboxed Blender.  Two properties carry the safety argument (each blocks escape alone):

* P1, closed value domain.  Values are None, bool, int (|x| < 2**63), finite float, capped str,
  list / tuple / dict[str, ...] of values (capped), ``Handle`` (an opaque op result) and nothing else:
  never a function, module, type, frame, generator, exception or bpy object.
* P2, no generic dispatch.  ``Attribute`` is evaluated only on the module aliases (``math``, ``random``,
  the kit via the manifest) and through a fixed method table keyed by (kind, name) after an isinstance
  check; ``Call.func`` is a name, a module-alias attribute or a method of a domain value; ``Subscript``
  applies to domain sequences and dicts.  This file never calls exec, eval, compile, __import__,
  getattr, setattr, delattr, type, vars, globals or locals (a test enforces it).

Because the script can read nothing but its own literals and the kit's literal constants, everything
it produces (names, sizes, triangle counts) carries only model-authored information.
"""
from __future__ import annotations

import ast
import json
import math
import random
import time

from . import opschema
from .errors import ScriptError
from .manifest import empty_manifest
from .predicates import compile_predicate
from .values import MAX_CONTAINER, MAX_INT, MAX_STR, Handle, Pred, copy_value

MAX_SOURCE_CHARS = 60_000
MAX_STEPS = 2_000_000
MAX_SECONDS = 10.0
MAX_CALL_DEPTH = 64
MAX_EXPR_DEPTH = 100
MAX_TOTAL_ELEMENTS = 10_000_000
MAX_TOTAL_CHARS = 50_000_000
MAX_PRINT_LINES = opschema.MAX_LOG_LINES
MAX_PRINT_CHARS = opschema.MAX_LOG_CHARS
_TIME_CHECK_EVERY = 10_000

_HINTS = {
    "Try": "errors cannot be caught; check values before using them",
    "TryStar": "errors cannot be caught; check values before using them",
    "With": "there are no files or context managers in build scripts",
    "AsyncWith": "there are no files or context managers in build scripts",
    "ClassDef": "use plain functions with lists, tuples and dicts",
    "Raise": "use assert <condition>, 'message' to stop with an error",
    "Global": "pass values as arguments and return results",
    "Nonlocal": "pass values as arguments and return results",
    "Yield": "return a list instead of yielding",
    "YieldFrom": "return a list instead of yielding",
    "Await": "there is no async code in build scripts",
    "AsyncFunctionDef": "use a plain def",
    "AsyncFor": "use a plain for loop",
    "Delete": "build a new list instead of deleting",
    "Starred": "write the elements out; * and ** unpacking are not available",
    "NamedExpr": "assign in a separate statement",
    "Match": "use if/elif",
    "Set": "use a list",
    "SetComp": "use a list comprehension",
    "DictComp": "build the dict with a loop",
    "Lambda": "lambdas are only supported as the predicate argument of paint_faces",
    "MatMult": "this operator is not available",
    "BitAnd": "bit operators are not available", "BitOr": "bit operators are not available",
    "BitXor": "bit operators are not available", "LShift": "bit operators are not available",
    "RShift": "bit operators are not available", "Invert": "bit operators are not available",
}
_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)
_ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign, ast.AnnAssign,
    ast.For, ast.While, ast.If, ast.Expr, ast.Pass, ast.Break, ast.Continue, ast.Assert, ast.Import, ast.ImportFrom,
    ast.alias, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Lambda, ast.IfExp, ast.Dict, ast.List, ast.Tuple, ast.Call,
    ast.keyword, ast.Constant, ast.Attribute, ast.Subscript, ast.Slice, ast.Name, ast.ListComp, ast.GeneratorExp,
    ast.comprehension, ast.JoinedStr, ast.FormattedValue, ast.Compare, ast.Load, ast.Store, ast.And, ast.Or,
    ast.Not, ast.USub, ast.UAdd, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In,
    ast.NotIn, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
)
_FORMAT_SPEC = None

_MATH_FUNCS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "atan2": math.atan2, "sqrt": math.sqrt, "radians": math.radians, "degrees": math.degrees, "floor": math.floor,
    "ceil": math.ceil, "hypot": math.hypot, "pow": math.pow, "exp": math.exp, "log": math.log, "fabs": math.fabs,
    "copysign": math.copysign, "fmod": math.fmod, "isclose": math.isclose,
}
_MATH_CONSTANTS = {"pi": math.pi, "tau": math.tau, "e": math.e}
_BUILTINS = frozenset({"range", "len", "min", "max", "abs", "round", "int", "float", "str", "bool", "list", "tuple",
                       "dict", "sum", "enumerate", "zip", "reversed", "sorted", "any", "all", "print"})
_RANDOM_FUNCS = frozenset({"random", "uniform", "randint", "choice", "shuffle", "seed"})
_BPY_HINT = "bpy is not available; use the kit and core functions listed in the prompt"


def _lineno(node):
    """The source line of an AST node, or 0 for nodes that carry none."""
    try:
        return node.lineno
    except AttributeError:
        return 0


class Recording:
    """The result of ``compile_script``: the op list, the print log and the serialised document."""

    __slots__ = ("doc", "text", "ops", "log")

    def __init__(self, doc, text):
        self.doc = doc
        self.text = text
        self.ops = doc["ops"]
        self.log = doc["log"]


class _Control(Exception):
    pass


class _Break(_Control):
    pass


class _Continue(_Control):
    pass


class _Return(_Control):
    def __init__(self, value):
        super().__init__()
        self.value = value


class _Scope:
    __slots__ = ("vars", "parent")

    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent


class _Func:
    __slots__ = ("name", "params", "defaults", "body", "line")

    def __init__(self, name, params, defaults, body, line):
        self.name = name
        self.params = params
        self.defaults = defaults
        self.body = body
        self.line = line


def _is_number(value):
    return isinstance(value, (int, float))


class _Interp:
    def __init__(self, manifest, seed):
        self.manifest = manifest
        self.rng = random.Random(seed)
        self.seed = seed
        self.ops = []
        self.log = []
        self.globals = {}
        self.funcs = {}
        self.modules = {}
        self.from_imports = {}
        self.scope = None
        self.line = 0
        self.steps = 0
        self.depth = 0
        self.call_depth = 0
        self.elems = 0
        self.chars = 0
        self.leaves = 0
        self.deadline = time.monotonic() + MAX_SECONDS
        self.probe = None
        self.core_names = frozenset(opschema.CORE_SIGS)

    # ------------------------------------------------------------------ errors and budgets
    def err(self, message, node=None):
        line = _lineno(node) or self.line
        return ScriptError(line, message)

    def too_much(self):
        return self.err("the script did too much work (limits: %d steps, %d seconds); use fewer or smaller loops"
                        % (MAX_STEPS, int(MAX_SECONDS)))

    def tick(self, node):
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise self.too_much()
        if self.steps % _TIME_CHECK_EVERY == 0 and time.monotonic() > self.deadline:
            raise self.too_much()
        line = _lineno(node)
        if line:
            self.line = line

    def charge(self, count):
        self.elems += count
        if count > MAX_CONTAINER or self.elems > MAX_TOTAL_ELEMENTS:
            raise self.err("the script built containers that are too large (limit: %d elements each, %d in total)"
                           % (MAX_CONTAINER, MAX_TOTAL_ELEMENTS))

    def charge_text(self, count):
        self.chars += count
        if count > MAX_STR or self.chars > MAX_TOTAL_CHARS:
            raise self.err("the script built strings that are too long (limit: %d characters each)" % MAX_STR)

    def checked(self, value):
        """Every number that enters the interpreter passes here: ints < 2**63 and finite floats only."""
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            if not -MAX_INT < value < MAX_INT:
                raise self.err("integer overflow: whole numbers must stay below 2**63")
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise self.err("the result is not a finite number")
        elif isinstance(value, complex):
            raise self.err("the result is not a real number")
        return value

    def handle_error(self, handle, node=None):
        return self.err("the result of %s(...) can only be passed to other kit/core functions; measurements are not "
                        "available while the script is recorded. Compute positions from the sizes you pass."
                        % handle.origin, node)

    def reject_handle(self, *values, node=None):
        for value in values:
            if isinstance(value, Handle):
                raise self.handle_error(value, node)

    # ------------------------------------------------------------------ scopes
    def lookup(self, name):
        scope = self.scope
        while scope is not None:
            if name in scope.vars:
                return True, scope.vars[name]
            scope = scope.parent
        if name in self.globals:
            return True, self.globals[name]
        return False, None

    def assign_name(self, name, value):
        if self.scope is None:
            self.globals[name] = value
        else:
            self.scope.vars[name] = value

    # ------------------------------------------------------------------ recording
    def encode(self, value, depth=1):
        """A domain value as JSON-grammar data (handles become ``$ref``)."""
        self.leaves += 1
        if self.leaves > opschema.MAX_LEAVES:
            raise self.err("the recorded calls hold more than %d values" % opschema.MAX_LEAVES)
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return self.checked(value)
        if isinstance(value, str):
            if len(value) > opschema.MAX_STRING:
                raise self.err("a string passed to a kit/core function is longer than %d characters"
                               % opschema.MAX_STRING)
            return value
        if isinstance(value, Handle):
            return {"$ref": value.id}
        if isinstance(value, Pred):
            return {"$pred": value.tree}
        if isinstance(value, (list, tuple)):
            if depth > opschema.MAX_NESTING:
                raise self.err("a value passed to a kit/core function is nested deeper than %d" % opschema.MAX_NESTING)
            return [self.encode(item, depth + 1) for item in value]
        raise self.err("a dict cannot be passed to a kit/core function; use a list or tuple")

    def record(self, op, origin, args, kwargs):
        if len(self.ops) >= opschema.MAX_OPS:
            raise self.err("the script makes more than %d kit/core calls; build fewer, larger parts" % opschema.MAX_OPS)
        ident = len(self.ops) + 1
        entry = {"id": ident, "line": self.line, "op": op, "args": [self.encode(a) for a in args],
                 "kwargs": {key: self.encode(val) for key, val in kwargs.items()}}
        self.ops.append(entry)
        return Handle(ident, origin)

    # ------------------------------------------------------------------ expression dispatch
    def ev(self, node):
        self.tick(node)
        self.depth += 1
        if self.depth > MAX_EXPR_DEPTH:
            raise self.err("the expression is nested too deeply")
        try:
            method = self._EXPR.get(node.__class__)
            if method is None:
                raise self.err("%s is not supported in build scripts" % node.__class__.__name__, node)
            value = method(self, node)
        finally:
            self.depth -= 1
        if self.probe is not None:
            self.probe(value)
        return value

    def ev_constant(self, node):
        value = node.value
        if value is None or isinstance(value, (bool, int, float)):
            return self.checked(value)
        if isinstance(value, str):
            if len(value) > MAX_STR:
                raise self.err("string literal is too long", node)
            return value
        raise self.err("this kind of literal is not supported in build scripts", node)

    def ev_name(self, node):
        found, value = self.lookup(node.id)
        if found:
            return value
        name = node.id
        if name in self.funcs or name in _BUILTINS or name in self.core_names or name in self.from_imports:
            raise self.err("%r is a function: call it with parentheses, it cannot be used as a value" % name, node)
        if name in self.modules:
            raise self.err("%r is a module: use it as %s.name" % (name, name), node)
        raise self.err("NameError: name '%s' is not defined" % name[:60], node)

    def ev_attribute(self, node):
        base = node.value
        if isinstance(base, ast.Name) and base.id in self.modules and not self.lookup(base.id)[0]:
            kind = self.modules[base.id]
            attr = node.attr
            if kind == "math":
                if attr in _MATH_CONSTANTS:
                    return _MATH_CONSTANTS[attr]
                raise self.err("math.%s is a function: call it with parentheses" % attr[:40], node)
            if kind == "random":
                raise self.err("random.%s is a function: call it with parentheses" % attr[:40], node)
            if attr in self.manifest.constants:
                return copy_value(self.manifest.constants[attr])
            if attr in self.manifest.signatures:
                raise self.err("%s.%s used as a value: kit functions can only be called" % (base.id, attr), node)
            raise self.err("%s.%s is not available in build scripts" % (base.id, attr[:40]), node)
        receiver = self.ev(base)
        self.reject_handle(receiver, node=node)
        raise self.err("attribute access is not supported in build scripts (only module functions, kit constants "
                       "and list/str/dict methods); use indexing or a helper function", node)

    def ev_list(self, node):
        items = [self.ev(item) for item in node.elts]
        self.charge(len(items))
        return items

    def ev_tuple(self, node):
        items = tuple(self.ev(item) for item in node.elts)
        self.charge(len(items))
        return items

    def ev_dict(self, node):
        out = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:
                raise self.err("** unpacking is not supported; write the keys out", node)
            key = self.ev(key_node)
            if not isinstance(key, str):
                raise self.err("dict keys must be strings", node)
            out[key] = self.ev(value_node)
        self.charge(len(out))
        return out

    def ev_boolop(self, node):
        is_and = isinstance(node.op, ast.And)
        value = None
        for item in node.values:
            value = self.ev(item)
            truth = self.truth(value, node)
            if is_and and not truth:
                return value
            if not is_and and truth:
                return value
        return value

    def truth(self, value, node=None):
        if isinstance(value, Handle):
            raise self.handle_error(value, node)
        return bool(value)

    def ev_ifexp(self, node):
        return self.ev(node.body) if self.truth(self.ev(node.test), node) else self.ev(node.orelse)

    def ev_unary(self, node):
        value = self.ev(node.operand)
        if isinstance(node.op, ast.Not):
            return not self.truth(value, node)
        self.reject_handle(value, node=node)
        if not _is_number(value):
            raise self.err("unary %s needs a number" % ("-" if isinstance(node.op, ast.USub) else "+"), node)
        return self.checked(-value if isinstance(node.op, ast.USub) else +value)

    def ev_binop(self, node):
        left = self.ev(node.left)
        right = self.ev(node.right)
        return self.binop(node.op, left, right, node)

    def binop(self, op, left, right, node):
        self.reject_handle(left, right, node=node)
        try:
            return self._binop(op, left, right, node)
        except ScriptError:
            raise
        except ZeroDivisionError:
            raise self.err("ZeroDivisionError: division by zero", node) from None
        except OverflowError:
            raise self.err("OverflowError: the result is too large", node) from None
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise self.err("%s: %s" % (exc.__class__.__name__, str(exc)[:120]), node) from None

    def _binop(self, op, left, right, node):
        if _is_number(left) and _is_number(right):
            if isinstance(op, ast.Add):
                return self.checked(left + right)
            if isinstance(op, ast.Sub):
                return self.checked(left - right)
            if isinstance(op, ast.Mult):
                return self.checked(left * right)
            if isinstance(op, ast.Div):
                return self.checked(left / right)
            if isinstance(op, ast.FloorDiv):
                return self.checked(left // right)
            if isinstance(op, ast.Mod):
                return self.checked(left % right)
            if isinstance(op, ast.Pow):
                if isinstance(left, int) and isinstance(right, int) and right > 64 and abs(left) > 1:
                    raise self.err("OverflowError: the result is too large", node)
                return self.checked(left ** right)
        if isinstance(op, ast.Add):
            if isinstance(left, str) and isinstance(right, str):
                self.charge_text(len(left) + len(right))
                return left + right
            if isinstance(left, list) and isinstance(right, list):
                self.charge(len(left) + len(right))
                return left + right
            if isinstance(left, tuple) and isinstance(right, tuple):
                self.charge(len(left) + len(right))
                return left + right
        if isinstance(op, ast.Mult):
            seq, count = (left, right) if not _is_number(left) else (right, left)
            if isinstance(seq, (str, list, tuple)) and isinstance(count, int):
                size = len(seq) * max(count, 0)
                if isinstance(seq, str):
                    self.charge_text(size)
                else:
                    self.charge(size)
                return seq * count
        if isinstance(op, ast.Mod) and isinstance(left, str):
            return self.percent(left, right, node)
        raise self.err("this operation is not supported between these types", node)

    def percent(self, template, args, node):
        if not _percent_ok(template):
            raise self.err("only simple % formats like %d, %s and %.2f are supported (no *, no %(name), widths up "
                           "to 999)", node)
        items = args if isinstance(args, tuple) else (args,)
        if not all(item is None or isinstance(item, (bool, int, float, str)) for item in items):
            raise self.err("% formatting accepts only numbers and strings", node)
        text = template % items
        self.charge_text(len(text))
        return text

    def ev_compare(self, node):
        left = self.ev(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = self.ev(comparator)
            if not self.compare(op, left, right, node):
                return False
            left = right
        return True

    def compare(self, op, left, right, node):
        if isinstance(op, (ast.Is, ast.IsNot)):
            if not (right is None or isinstance(right, bool)):
                raise self.err("use == to compare values; 'is' only works with None, True and False", node)
            same = left is right
            return same if isinstance(op, ast.Is) else not same
        self.reject_handle(left, right, node=node)
        try:
            if isinstance(op, ast.Eq):
                return left == right
            if isinstance(op, ast.NotEq):
                return left != right
            if isinstance(op, (ast.In, ast.NotIn)):
                if not isinstance(right, (list, tuple, str, dict)):
                    raise self.err("'in' needs a list, tuple, string or dict on the right", node)
                if isinstance(right, str) and not isinstance(left, str):
                    raise self.err("'in' on a string needs a string on the left", node)
                found = left in right
                return found if isinstance(op, ast.In) else not found
            if isinstance(op, ast.Lt):
                return left < right
            if isinstance(op, ast.LtE):
                return left <= right
            if isinstance(op, ast.Gt):
                return left > right
            return left >= right
        except TypeError as exc:
            raise self.err("TypeError: %s" % str(exc)[:120], node) from None

    def iterable(self, value, node=None):
        self.reject_handle(value, node=node)
        if isinstance(value, list):
            return list(value)
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, str):
            self.charge(len(value))
            return list(value)
        if isinstance(value, dict):
            return list(value)
        raise self.err("TypeError: this value is not iterable (use a list, tuple, string, dict or range)", node)

    def ev_subscript(self, node):
        base = self.ev(node.value)
        self.reject_handle(base, node=node)
        if isinstance(node.slice, ast.Slice):
            parts = []
            for part in (node.slice.lower, node.slice.upper, node.slice.step):
                value = None if part is None else self.ev(part)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                    raise self.err("slice bounds must be integers", node)
                parts.append(value)
            if not isinstance(base, (list, tuple, str)):
                raise self.err("only lists, tuples and strings can be sliced", node)
            try:
                result = base[slice(*parts)]
            except ValueError as exc:
                raise self.err("ValueError: %s" % str(exc)[:80], node) from None
            self.charge(len(result))
            return result
        index = self.ev(node.slice)
        self.reject_handle(index, node=node)
        try:
            if isinstance(base, (list, tuple, str)):
                if isinstance(index, bool) or not isinstance(index, int):
                    raise self.err("TypeError: list/string indices must be integers", node)
                return base[index]
            if isinstance(base, dict):
                if not isinstance(index, str):
                    raise self.err("TypeError: dict keys must be strings", node)
                return base[index]
        except IndexError:
            raise self.err("IndexError: index %d out of range" % index, node) from None
        except KeyError:
            raise self.err("KeyError: %s" % repr(index[:40]), node) from None
        raise self.err("this value cannot be indexed", node)

    def ev_joined(self, node):
        parts = []
        for item in node.values:
            if isinstance(item, ast.Constant):
                parts.append(item.value)
            elif isinstance(item, ast.FormattedValue):
                parts.append(self.format_value(item))
            else:
                raise self.err("unsupported f-string part", node)
        text = "".join(parts)
        self.charge_text(len(text))
        return text

    def format_value(self, node):
        value = self.ev(node.value)
        self.reject_handle(value, node=node)
        if node.conversion != -1:
            raise self.err("f-string conversions (!r, !s) are not supported", node)
        spec = ""
        if node.format_spec is not None:
            pieces = node.format_spec.values
            if not all(isinstance(piece, ast.Constant) and isinstance(piece.value, str) for piece in pieces):
                raise self.err("f-string format specs must be constants such as .2f", node)
            spec = "".join(piece.value for piece in pieces)
        if not spec:
            return self.show(value)
        if not (value is None or isinstance(value, (int, float, str))) or not _safe_spec(spec):
            raise self.err("unsupported f-string format %r; use simple specs like .2f, 03d or >8" % spec[:20], node)
        try:
            return format(value, spec)
        except (ValueError, TypeError, OverflowError) as exc:
            raise self.err("%s: %s" % (exc.__class__.__name__, str(exc)[:80]), node) from None

    def show(self, value, depth=0):
        """Text for print()/f-strings: plain repr for scalars, ``<result of op>`` for handles."""
        if depth > 8:
            return "..."
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            inner = ", ".join(self.show_item(item, depth + 1) for item in value[:200])
            return "[%s]" % inner if isinstance(value, list) else "(%s)" % inner
        if isinstance(value, dict):
            return "{%s}" % ", ".join("%r: %s" % (key, self.show_item(item, depth + 1)) for key, item in list(value.items())[:200])
        return repr(value)

    def show_item(self, value, depth):
        return repr(value) if isinstance(value, str) else self.show(value, depth)

    def ev_comp(self, node):
        out = []
        saved = self.scope
        self.scope = _Scope(saved)
        try:
            self.comprehend(node.generators, 0, node.elt, out)
        finally:
            self.scope = saved
        return out

    def comprehend(self, generators, index, element, out):
        if index == len(generators):
            out.append(self.ev(element))
            self.charge(1)
            return
        generator = generators[index]
        for item in self.iterable(self.ev(generator.iter), generator.iter):
            self.assign(generator.target, item)
            if all(self.truth(self.ev(test), test) for test in generator.ifs):
                self.comprehend(generators, index + 1, element, out)

    def ev_lambda(self, node):
        raise self.err("a lambda is only supported as the predicate argument of paint_faces, for example "
                       "lambda p: p.center.z > 0.5", node)

    # ------------------------------------------------------------------ calls
    def ev_call(self, node):
        func = node.func
        if isinstance(func, ast.Name):
            return self.call_name(func.id, node)
        if isinstance(func, ast.Attribute):
            base = func.value
            if isinstance(base, ast.Name) and base.id in self.modules and not self.lookup(base.id)[0]:
                return self.call_module(self.modules[base.id], base.id, func.attr, node)
            receiver = self.ev(base)
            return self.call_method(receiver, func.attr, node)
        raise self.err("only named functions can be called (a value, lambda or call result cannot)", node)

    def call_name(self, name, node):
        found, value = self.lookup(name)
        if found:
            raise self.err("%r is a value, not a function: only defined functions, builtins and kit/core functions "
                           "can be called" % name, node)
        if name in self.funcs:
            return self.call_user(self.funcs[name], node)
        if name in self.from_imports:
            kind, attr = self.from_imports[name]
            return self.call_module(kind, kind, attr, node)
        if name in self.core_names:
            return self.call_recorded("core." + name, "core." + name, opschema.CORE_SIGS[name],
                                      opschema.CORE_PREDICATE_PARAMS.get(name, ()), node)
        if name in _BUILTINS:
            return self.call_builtin(name, node)
        if name in self.modules:
            raise self.err("%r is a module, not a function" % name, node)
        raise self.err("NameError: name '%s' is not defined" % name[:60], node)

    def call_module(self, kind, alias, attr, node):
        if kind == "kit":
            sig = self.manifest.signatures.get(attr)
            if sig is None:
                if attr in self.manifest.constants:
                    raise self.err("%s.%s is a constant, not a function" % (alias, attr), node)
                raise self.err("%s.%s is not available in build scripts; the harness exports and renders, and only "
                               "the functions in the kit API list can be called" % (alias, attr[:40]), node)
            signature = (sig.params + sig.kwonly, sig.required, sig.varkw or sig.varargs)
            return self.call_recorded("kit." + attr, "%s.%s" % (alias, attr), signature,
                                      self.manifest.predicate_params.get(attr, ()), node, sig)
        args, kwargs = self.plain_arguments(node)
        for value in list(args) + list(kwargs.values()):
            if not (_is_number(value) or (kind == "random" and isinstance(value, (str, list, tuple)))):
                self.reject_handle(value, node=node)
                raise self.err("%s.%s takes numbers%s" % (alias, attr, " (or a sequence)" if kind == "random" else ""),
                               node)
        try:
            if kind == "math":
                function = _MATH_FUNCS.get(attr)
                if function is None:
                    raise self.err("math.%s is not available in build scripts" % attr[:40], node)
                return self.checked(function(*args, **kwargs))
            return self.call_random(attr, args, kwargs, node)
        except ScriptError:
            raise
        except (TypeError, ValueError, OverflowError, ZeroDivisionError, IndexError) as exc:
            raise self.err("%s: %s" % (exc.__class__.__name__, str(exc)[:100]), node) from None

    def call_random(self, attr, args, kwargs, node):
        if attr not in _RANDOM_FUNCS or kwargs:
            raise self.err("random.%s is not available in build scripts" % attr[:40], node)
        if attr == "random":
            return self.rng.random()
        if attr == "uniform":
            return self.checked(self.rng.uniform(*args))
        if attr == "randint":
            return self.rng.randint(*args)
        if attr == "seed":
            if len(args) != 1 or isinstance(args[0], list) or isinstance(args[0], tuple):
                raise self.err("random.seed takes one number or string", node)
            self.rng.seed(args[0])
            return None
        sequence = args[0] if len(args) == 1 else None
        if attr == "choice":
            if isinstance(sequence, (list, tuple, str)) and len(sequence) > 0:
                return self.rng.choice(sequence)
            raise self.err("random.choice needs a non-empty list, tuple or string", node)
        if isinstance(sequence, list):
            self.rng.shuffle(sequence)
            return None
        raise self.err("random.shuffle needs a list", node)

    def plain_arguments(self, node):
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(kw.arg is None for kw in node.keywords):
            raise self.err("* and ** argument unpacking is not supported; write the arguments out", node)
        args = [self.ev(arg) for arg in node.args]
        kwargs = {kw.arg: self.ev(kw.value) for kw in node.keywords}
        return args, kwargs

    def argument_value(self, argument, is_predicate, node):
        if isinstance(argument, ast.Lambda):
            if not is_predicate:
                raise self.err("a lambda is only supported as the predicate argument of paint_faces", argument)
            self.tick(argument)
            aliases = tuple(name for name, kind in self.modules.items() if kind == "math")
            return Pred(compile_predicate(argument, self.capture, aliases))
        if is_predicate:
            raise self.err("the predicate argument must be written inline as a lambda, "
                           "for example lambda p: p.center.z > 0.5", argument)
        return self.ev(argument)

    def capture(self, name, line):
        found, value = self.lookup(name)
        if not found:
            raise ScriptError(line, "NameError: name '%s' is not defined" % name[:60])
        if isinstance(value, Handle):
            raise self.handle_error(value)
        return value

    def call_recorded(self, op, origin, signature, predicate_params, node, kit_sig=None):
        params, required, loose = signature
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(kw.arg is None for kw in node.keywords):
            raise self.err("* and ** argument unpacking is not supported; write the arguments out", node)
        real = ", ".join(params) if params else "(no parameters)"
        if len(node.args) > len(params) and not (kit_sig is not None and kit_sig.varargs):
            raise self.err("%s takes at most %d positional arguments (%s)" % (origin, len(params), real), node)
        args = []
        for position, argument in enumerate(node.args):
            name = params[position] if position < len(params) else ""
            args.append(self.argument_value(argument, name in predicate_params, node))
        kwargs = {}
        for keyword in node.keywords:
            if keyword.arg not in params and not loose:
                raise self.err("%s has no parameter %r; its parameters are: %s" % (origin, keyword.arg[:40], real), node)
            if keyword.arg in params and params.index(keyword.arg) < len(args):
                raise self.err("%s got %r twice" % (origin, keyword.arg), node)
            kwargs[keyword.arg] = self.argument_value(keyword.value, keyword.arg in predicate_params, node)
        missing = [name for name in params[:required] if params.index(name) >= len(args) and name not in kwargs]
        if missing:
            raise self.err("%s is missing %s; its parameters are: %s" % (origin, ", ".join(repr(m) for m in missing), real),
                           node)
        self.line = _lineno(node) or self.line
        return self.record(op, origin, args, kwargs)

    def call_user(self, func, node):
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(kw.arg is None for kw in node.keywords):
            raise self.err("* and ** argument unpacking is not supported; write the arguments out", node)
        if len(node.args) > len(func.params):
            raise self.err("%s() takes at most %d arguments (%s)" % (func.name, len(func.params), ", ".join(func.params)),
                           node)
        bound = {}
        for name, argument in zip(func.params, node.args, strict=False):
            bound[name] = self.ev(argument)
        for keyword in node.keywords:
            if keyword.arg not in func.params:
                raise self.err("%s() has no parameter %r; its parameters are: %s"
                               % (func.name, keyword.arg[:40], ", ".join(func.params)), node)
            if keyword.arg in bound:
                raise self.err("%s() got %r twice" % (func.name, keyword.arg), node)
            bound[keyword.arg] = self.ev(keyword.value)
        for name in func.params:
            if name not in bound:
                if name in func.defaults:
                    bound[name] = copy_value(func.defaults[name])
                else:
                    raise self.err("%s() is missing the argument %r" % (func.name, name), node)
        if self.call_depth >= MAX_CALL_DEPTH:
            raise self.err("functions are nested more than %d calls deep (recursion is limited)" % MAX_CALL_DEPTH, node)
        scope = _Scope(None)
        scope.vars.update(bound)
        saved, saved_depth = self.scope, self.depth
        self.scope = scope
        self.call_depth += 1
        self.depth = 0
        try:
            self.run_block(func.body)
        except _Return as done:
            return done.value
        except (_Break, _Continue):
            raise self.err("'break' or 'continue' outside a loop", node) from None
        finally:
            self.call_depth -= 1
            self.scope = saved
            self.depth = saved_depth
        return None

    # ------------------------------------------------------------------ builtins
    def call_builtin(self, name, node):
        args, kwargs = self.plain_arguments(node)
        for value in list(args) + list(kwargs.values()):
            if isinstance(value, Handle) and name != "print":
                raise self.handle_error(value, node)
        try:
            return run_builtin(self, name, args, kwargs, node)
        except ScriptError:
            raise
        except ZeroDivisionError:
            raise self.err("ZeroDivisionError: division by zero", node) from None
        except OverflowError:
            raise self.err("OverflowError: the result is too large", node) from None
        except (TypeError, ValueError, KeyError, IndexError) as exc:
            raise self.err("%s: %s" % (exc.__class__.__name__, str(exc)[:120]), node) from None

    def call_method(self, receiver, name, node):
        self.reject_handle(receiver, node=node)
        args, kwargs = self.plain_arguments(node)
        stores = isinstance(receiver, list) and name in ("append", "insert", "extend")  # a list may hold parts
        for value in list(args) + list(kwargs.values()):
            if isinstance(value, Handle) and not stores:
                raise self.handle_error(value, node)
        try:
            return method_call(self, receiver, name, args, kwargs, node)
        except ScriptError:
            raise
        except (TypeError, ValueError, KeyError, IndexError, OverflowError) as exc:
            raise self.err("%s: %s" % (exc.__class__.__name__, str(exc)[:120]), node) from None

    # ------------------------------------------------------------------ statements
    def run_block(self, statements):
        for statement in statements:
            self.exec_statement(statement)

    def exec_statement(self, node):
        self.tick(node)
        method = self._STMT.get(node.__class__)
        if method is None:
            raise self.err("%s is not supported in build scripts" % node.__class__.__name__, node)
        method(self, node)

    def assign(self, target, value):
        if isinstance(target, ast.Name):
            self.assign_name(target.id, value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            if isinstance(value, Handle):
                raise self.handle_error(value, target)
            if not isinstance(value, (list, tuple)):
                raise self.err("TypeError: only lists and tuples can be unpacked", target)
            if len(value) != len(target.elts):
                raise self.err("ValueError: expected %d values to unpack, got %d" % (len(target.elts), len(value)), target)
            for sub, item in zip(target.elts, value, strict=False):
                self.assign(sub, item)
        elif isinstance(target, ast.Subscript):
            container = self.ev(target.value)
            self.reject_handle(container, node=target)
            if isinstance(target.slice, ast.Slice):
                raise self.err("slice assignment is not supported", target)
            index = self.ev(target.slice)
            try:
                if isinstance(container, list) and isinstance(index, int) and not isinstance(index, bool):
                    container[index] = value
                elif isinstance(container, dict) and isinstance(index, str):
                    if index not in container:
                        self.charge(1)
                    container[index] = value
                else:
                    raise self.err("only list items (by integer) and dict items (by string) can be assigned", target)
            except IndexError:
                raise self.err("IndexError: assignment index out of range", target) from None
        else:
            raise self.err("only names, unpacking and list/dict items can be assigned", target)

    def st_assign(self, node):
        value = self.ev(node.value)
        for target in node.targets:
            self.assign(target, value)

    def st_annassign(self, node):
        if node.value is not None:
            self.assign(node.target, self.ev(node.value))

    def st_augassign(self, node):
        if isinstance(node.target, ast.Name):
            found, current = self.lookup(node.target.id)
            if not found:
                raise self.err("NameError: name '%s' is not defined" % node.target.id[:60], node)
            self.assign(node.target, self.binop(node.op, current, self.ev(node.value), node))
            return
        if isinstance(node.target, ast.Subscript):
            container = self.ev(node.target.value)
            self.reject_handle(container, node=node)
            index = self.ev(node.target.slice)
            current = self.ev_subscript_value(container, index, node)
            combined = self.binop(node.op, current, self.ev(node.value), node)
            if isinstance(container, list) and isinstance(index, int) and not isinstance(index, bool):
                container[index] = combined
            elif isinstance(container, dict) and isinstance(index, str):
                container[index] = combined
            else:
                raise self.err("only list and dict items can be assigned", node)
            return
        raise self.err("only names and list/dict items can be assigned", node)

    def ev_subscript_value(self, container, index, node):
        try:
            if isinstance(container, list) and isinstance(index, int) and not isinstance(index, bool):
                return container[index]
            if isinstance(container, dict) and isinstance(index, str):
                return container[index]
        except (IndexError, KeyError):
            raise self.err("IndexError/KeyError: no such item", node) from None
        raise self.err("only list and dict items can be assigned", node)

    def st_expr(self, node):
        self.ev(node.value)

    def st_return(self, node):
        raise _Return(None if node.value is None else self.ev(node.value))

    def st_if(self, node):
        if self.truth(self.ev(node.test), node):
            self.run_block(node.body)
        else:
            self.run_block(node.orelse)

    def st_for(self, node):
        for item in self.iterable(self.ev(node.iter), node.iter):
            self.assign(node.target, item)
            try:
                self.run_block(node.body)
            except _Break:
                break
            except _Continue:
                continue

    def st_while(self, node):
        while self.truth(self.ev(node.test), node):
            try:
                self.run_block(node.body)
            except _Break:
                break
            except _Continue:
                continue

    def st_break(self, node):
        raise _Break()

    def st_continue(self, node):
        raise _Continue()

    def st_pass(self, node):
        return None

    def st_assert(self, node):
        if not self.truth(self.ev(node.test), node):
            message = "" if node.msg is None else self.show(self.ev(node.msg))
            raise self.err("AssertionError: %s" % message[:300], node)

    def st_import(self, node):
        for item in node.names:
            self.import_module(item.name, item.asname, node)

    def import_module(self, name, alias, node):
        root = name.split(".")[0]
        bound = alias or name
        if name in ("math", "random"):
            self.modules[bound] = name
        elif root in ("bpy", "bmesh", "mathutils"):
            raise self.err("import %s: %s" % (root, _BPY_HINT), node)
        elif self.manifest.kit_module and name == self.manifest.kit_module:
            self.modules[bound] = "kit"
        else:
            raise self.err("import %s is not supported; only math, random%s can be imported"
                           % (name[:40], " and " + self.manifest.kit_module if self.manifest.kit_module else ""), node)

    def st_importfrom(self, node):
        module = node.module or ""
        if node.level:
            raise self.err("relative imports are not supported", node)
        root = module.split(".")[0]
        if root in ("bpy", "bmesh", "mathutils"):
            raise self.err("from %s import ...: %s" % (root, _BPY_HINT), node)
        if module not in ("math", "random"):
            if self.manifest.kit_module and module == self.manifest.kit_module:
                raise self.err("from %s import ... is not supported; write import %s and call %s.name(...)"
                               % (module, module, module), node)
            raise self.err("from %s import ... is not supported; only math and random can be imported" % module[:40], node)
        for item in node.names:
            if item.name == "*":
                raise self.err("import * is not supported; name what you import", node)
            known = _MATH_FUNCS if module == "math" else _RANDOM_FUNCS
            if item.name in _MATH_CONSTANTS and module == "math":
                self.globals[item.asname or item.name] = _MATH_CONSTANTS[item.name]
            elif item.name in known:
                self.from_imports[item.asname or item.name] = (module, item.name)
            else:
                raise self.err("%s.%s is not available in build scripts" % (module, item.name[:40]), node)

    def st_funcdef(self, node):
        if self.scope is not None:
            raise self.err("define helper functions at the top level, not inside another function", node)
        args = node.args
        params = [arg.arg for arg in args.args]
        defaults = {}
        offset = len(params) - len(args.defaults)
        for position, default in enumerate(args.defaults):
            defaults[params[offset + position]] = self.ev(default)
        self.funcs[node.name] = _Func(node.name, params, defaults, node.body, node.lineno)

    _EXPR = {}
    _STMT = {}


def _digit_runs(text):
    """Every run of digits that follows a ``%`` flag or a ``.`` in a % format string, as ints."""
    runs = []
    current = ""
    for char in text:
        if char.isdigit():
            current += char
        else:
            if current:
                runs.append(int(current))
            current = ""
    if current:
        runs.append(int(current))
    return runs


def _percent_ok(template):
    """True when every % conversion in ``template`` is simple: no ``*`` width, no ``%(name)``, numbers <= 999."""
    index = 0
    while index < len(template):
        if template[index] != "%":
            index += 1
            continue
        index += 1
        if index < len(template) and template[index] == "%":
            index += 1
            continue
        digits = ""
        while index < len(template) and template[index] in "-+ #0123456789.*(":
            char = template[index]
            if char in "*(":
                return False
            if char.isdigit():
                digits += char
                if len(digits) > 3:
                    return False
            else:
                digits = ""
            index += 1
    return True


def _safe_spec(spec):
    if len(spec) > 12:
        return False
    return all(char in "<>^=+- #0123456789.,_bdeEfFgGnosxX%" for char in spec) and all(
        number <= 999 for number in _digit_runs(spec))


def run_builtin(interp, name, args, kwargs, node):
    """The callable builtins as fixed wrappers (no lookup by computed name)."""
    allowed_kw = {"sorted": ("reverse",), "enumerate": ("start",), "sum": ("start",), "round": ("ndigits",),
                  "int": ("base",)}
    if set(kwargs) - set(allowed_kw.get(name, ())):
        raise interp.err("%s() does not take keyword arguments like %s" % (name, ", ".join(sorted(kwargs))), node)
    n = len(args)

    def need(low, high):
        if not low <= n <= high:
            raise interp.err("%s() takes %s argument%s" % (name, low if low == high else "%d to %d" % (low, high),
                                                         "" if high == 1 else "s"), node)

    if name == "range":
        need(1, 3)
        if not all(isinstance(a, int) and not isinstance(a, bool) for a in args):
            raise interp.err("range() takes integers", node)
        span = range(*args)
        if len(span) > MAX_CONTAINER:
            raise interp.err("range() is too long (limit: %d elements)" % MAX_CONTAINER, node)
        interp.charge(len(span))
        return list(span)
    if name == "len":
        need(1, 1)
        if not isinstance(args[0], (str, list, tuple, dict)):
            raise interp.err("len() needs a string, list, tuple or dict", node)
        return len(args[0])
    if name in ("min", "max"):
        pool = interp.iterable(args[0], node) if n == 1 else list(args)
        if not pool:
            raise interp.err("%s() of an empty sequence" % name, node)
        return min(pool) if name == "min" else max(pool)
    if name == "abs":
        need(1, 1)
        if not _is_number(args[0]):
            raise interp.err("abs() needs a number", node)
        return interp.checked(abs(args[0]))
    if name == "round":
        need(1, 2)
        digits = kwargs.get("ndigits", args[1] if n == 2 else None)
        if not _is_number(args[0]) or (digits is not None and (not isinstance(digits, int) or abs(digits) > 20)):
            raise interp.err("round(number, digits) takes a number and small integer digits", node)
        return interp.checked(round(args[0]) if digits is None else round(args[0], digits))
    if name == "int":
        need(1, 2)
        base = kwargs.get("base", args[1] if n == 2 else None)
        value = args[0]
        if base is not None:
            if not isinstance(value, str) or not isinstance(base, int):
                raise interp.err("int(text, base) needs a string and an integer base", node)
            return interp.checked(int(value, base))
        if isinstance(value, str) and len(value) > 40:
            raise interp.err("int() of a very long string", node)
        if not (isinstance(value, (bool, int, float, str))):
            raise interp.err("int() needs a number or string", node)
        return interp.checked(int(value))
    if name == "float":
        need(1, 1)
        if not isinstance(args[0], (bool, int, float, str)):
            raise interp.err("float() needs a number or string", node)
        return interp.checked(float(args[0]))
    if name == "str":
        need(1, 1)
        text = interp.show(args[0])
        interp.charge_text(len(text))
        return text
    if name == "bool":
        need(1, 1)
        return bool(args[0])
    if name in ("list", "tuple"):
        need(0, 1)
        items = interp.iterable(args[0], node) if n else []
        interp.charge(len(items))
        return items if name == "list" else tuple(items)
    if name == "dict":
        need(0, 1)
        if not n:
            return {}
        source = args[0]
        if isinstance(source, dict):
            interp.charge(len(source))
            return dict(source)
        pairs = interp.iterable(source, node)
        out = {}
        for pair in pairs:
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[0], str)):
                raise interp.err("dict() needs a list of (string, value) pairs", node)
            out[pair[0]] = pair[1]
        interp.charge(len(out))
        return out
    if name == "sum":
        need(1, 2)
        total = kwargs.get("start", args[1] if n == 2 else 0)
        for item in interp.iterable(args[0], node):
            if not (_is_number(item) and _is_number(total)):
                raise interp.err("sum() needs numbers", node)
            total = interp.checked(total + item)
        return total
    if name == "enumerate":
        need(1, 2)
        start = kwargs.get("start", args[1] if n == 2 else 0)
        if not isinstance(start, int) or isinstance(start, bool):
            raise interp.err("enumerate() start must be an integer", node)
        items = interp.iterable(args[0], node)
        interp.charge(len(items))
        return [(index, item) for index, item in enumerate(items, start)]
    if name == "zip":
        lists = [interp.iterable(a, node) for a in args]
        out = list(zip(*lists, strict=False)) if lists else []
        interp.charge(len(out))
        return out
    if name == "reversed":
        need(1, 1)
        if not isinstance(args[0], (list, tuple, str)):
            raise interp.err("reversed() needs a list, tuple or string", node)
        items = interp.iterable(args[0], node)
        items.reverse()
        return items
    if name == "sorted":
        need(1, 1)
        reverse = kwargs.get("reverse", False)
        items = interp.iterable(args[0], node)
        interp.charge(len(items))
        items.sort(reverse=bool(reverse))
        return items
    if name == "any":
        need(1, 1)
        return any(bool(item) for item in interp.iterable(args[0], node))
    if name == "all":
        need(1, 1)
        return all(bool(item) for item in interp.iterable(args[0], node))
    if name == "print":
        if len(interp.log) < MAX_PRINT_LINES:
            interp.log.append(" ".join(interp.show(a) for a in args)[:MAX_PRINT_CHARS])
        return None
    raise interp.err("NameError: name '%s' is not defined" % name[:60], node)


def method_call(interp, receiver, name, args, kwargs, node):
    """Methods on str, list, tuple and dict through a fixed table; anything else is rejected."""
    n = len(args)

    def need(low, high):
        if not low <= n <= high:
            raise interp.err("%s() takes %s argument%s" % (name, low if low == high else "%d to %d" % (low, high),
                                                         "" if high == 1 else "s"), node)

    if kwargs and not (name in ("sort",) and set(kwargs) <= {"reverse"}):
        raise interp.err(".%s() does not take keyword arguments" % name, node)
    if isinstance(receiver, str):
        if name in ("upper", "lower"):
            need(0, 0)
            return receiver.upper() if name == "upper" else receiver.lower()
        if name == "strip":
            need(0, 1)
            if args and not isinstance(args[0], str):
                raise interp.err("strip() takes a string of characters", node)
            return receiver.strip(*args)
        if name == "join":
            need(1, 1)
            items = interp.iterable(args[0], node)
            if not all(isinstance(item, str) for item in items):
                raise interp.err("join() needs a list of strings", node)
            size = sum(len(item) for item in items) + len(receiver) * max(len(items) - 1, 0)
            interp.charge_text(size)
            return receiver.join(items)
        if name == "replace":
            need(2, 3)
            old, new = args[0], args[1]
            if not (isinstance(old, str) and isinstance(new, str)):
                raise interp.err("replace() needs strings", node)
            count = receiver.count(old) if old else len(receiver) + 1
            if len(receiver) + count * max(len(new) - len(old), 0) > MAX_STR:
                raise interp.err("the replaced string would be too long", node)
            interp.charge_text(len(receiver))
            return receiver.replace(*args)
        if name in ("startswith", "endswith"):
            need(1, 1)
            if not (isinstance(args[0], str) or (isinstance(args[0], tuple) and all(isinstance(a, str) for a in args[0]))):
                raise interp.err("%s() needs a string" % name, node)
            return receiver.startswith(args[0]) if name == "startswith" else receiver.endswith(args[0])
        if name == "split":
            need(0, 2)
            if args and (not isinstance(args[0], str) or not args[0]):
                raise interp.err("split() separator must be a non-empty string", node)
            parts = receiver.split(*args)
            interp.charge(len(parts))
            return parts
        if name == "zfill":
            need(1, 1)
            if not isinstance(args[0], int) or isinstance(args[0], bool) or not 0 <= args[0] <= 1000:
                raise interp.err("zfill() width must be between 0 and 1000", node)
            return receiver.zfill(args[0])
    elif isinstance(receiver, list):
        if name == "append":
            need(1, 1)
            interp.charge(1)
            receiver.append(args[0])
            return None
        if name == "extend":
            need(1, 1)
            items = interp.iterable(args[0], node)
            interp.charge(len(items) + len(receiver))
            receiver.extend(items)
            return None
        if name == "pop":
            need(0, 1)
            return receiver.pop(*args)
        if name == "insert":
            need(2, 2)
            if not isinstance(args[0], int) or isinstance(args[0], bool):
                raise interp.err("insert() index must be an integer", node)
            interp.charge(1)
            receiver.insert(args[0], args[1])
            return None
        if name == "sort":
            need(0, 0)
            receiver.sort(reverse=bool(kwargs.get("reverse", False)))
            return None
        if name == "reverse":
            need(0, 0)
            receiver.reverse()
            return None
    if isinstance(receiver, (list, tuple)):
        if name == "index":
            need(1, 1)
            return receiver.index(args[0])
        if name == "count":
            need(1, 1)
            return receiver.count(args[0])
    elif isinstance(receiver, dict):
        if name == "get":
            need(1, 2)
            if not isinstance(args[0], str):
                raise interp.err("dict keys must be strings", node)
            return receiver.get(args[0], args[1] if n == 2 else None)
        if name in ("keys", "values", "items"):
            need(0, 0)
            out = list(receiver) if name == "keys" else (
                list(receiver.values()) if name == "values" else list(receiver.items()))
            interp.charge(len(out))
            return out
    raise interp.err("%s has no supported method .%s(); supported: str join/upper/lower/strip/replace/startswith/"
                     "endswith/split/zfill, list append/extend/pop/insert/sort/reverse/index/count, dict "
                     "get/keys/values/items" % (_kind_name(receiver), name[:40]), node)


def _kind_name(value):
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, tuple):
        return "a tuple"
    if isinstance(value, dict):
        return "a dict"
    return "this value"


_Interp._EXPR = {
    ast.Constant: _Interp.ev_constant, ast.Name: _Interp.ev_name, ast.Attribute: _Interp.ev_attribute,
    ast.List: _Interp.ev_list, ast.Tuple: _Interp.ev_tuple, ast.Dict: _Interp.ev_dict, ast.BoolOp: _Interp.ev_boolop,
    ast.IfExp: _Interp.ev_ifexp, ast.UnaryOp: _Interp.ev_unary, ast.BinOp: _Interp.ev_binop,
    ast.Compare: _Interp.ev_compare, ast.Subscript: _Interp.ev_subscript, ast.JoinedStr: _Interp.ev_joined,
    ast.ListComp: _Interp.ev_comp, ast.GeneratorExp: _Interp.ev_comp, ast.Call: _Interp.ev_call,
    ast.Lambda: _Interp.ev_lambda,
}
_Interp._STMT = {
    ast.Assign: _Interp.st_assign, ast.AnnAssign: _Interp.st_annassign, ast.AugAssign: _Interp.st_augassign,
    ast.Expr: _Interp.st_expr, ast.Return: _Interp.st_return, ast.If: _Interp.st_if, ast.For: _Interp.st_for,
    ast.While: _Interp.st_while, ast.Break: _Interp.st_break, ast.Continue: _Interp.st_continue,
    ast.Pass: _Interp.st_pass, ast.Assert: _Interp.st_assert, ast.Import: _Interp.st_import,
    ast.ImportFrom: _Interp.st_importfrom, ast.FunctionDef: _Interp.st_funcdef,
}


def _check_tree(tree):
    """Reject constructs outside the subset before anything runs, naming the line and giving a hint."""
    for node in ast.walk(tree):
        name = node.__class__.__name__
        line = _lineno(node)
        if not isinstance(node, _ALLOWED_NODES):
            hint = _HINTS.get(name, "use plain assignments, loops, functions and kit/core calls")
            raise ScriptError(line, "%s is not supported in build scripts; %s" % (name, hint))
        if isinstance(node, ast.Constant) and not (
                node.value is None or isinstance(node.value, (bool, int, float, str))):
            raise ScriptError(line, "this kind of literal (bytes, complex, ...) is not supported in build scripts")
        if isinstance(node, ast.FunctionDef):
            args = node.args
            if node.decorator_list:
                raise ScriptError(line, "decorators are not supported in build scripts; remove them")
            if args.vararg or args.kwarg or args.kwonlyargs or args.posonlyargs:
                raise ScriptError(line, "functions take plain positional parameters only (no *args, **kwargs or "
                                        "keyword-only parameters)")
        if isinstance(node, (ast.For, ast.While)) and node.orelse:
            raise ScriptError(line, "else on a loop is not supported in build scripts; restructure the loop")
        if isinstance(node, ast.comprehension) and node.is_async:
            raise ScriptError(line, "async comprehensions are not supported in build scripts")
        if isinstance(node, ast.BinOp) and not isinstance(node.op, _BINOPS):
            raise ScriptError(line, "%s is not supported in build scripts; %s"
                              % (node.op.__class__.__name__, _HINTS.get(node.op.__class__.__name__, "use + - * / // % **")))
        if isinstance(node, ast.UnaryOp) and not isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
            raise ScriptError(line, "bit operators are not available in build scripts")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
            raise ScriptError(line, "NameError: name '__import__' is not defined")


def _check_top_level(tree):
    builds = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build"]
    if len(builds) != 1:
        raise ScriptError(0, "the script must define exactly one top-level 'def build():'")
    build = builds[0]
    args = build.args
    if args.args or args.defaults:
        raise ScriptError(build.lineno, "def build() takes no parameters")
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.AnnAssign, ast.Pass)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        raise ScriptError(_lineno(node), "only imports, function definitions and constant assignments are "
                                                      "allowed at the top level; put the code inside def build()")


def compile_script(source, manifest=None, seed=0, probe=None):
    """Record ``source`` into an op list.  Raises ``ScriptError`` (and nothing else) for anything it cannot accept.

    Pure and deterministic: the result depends only on ``source``, ``manifest`` and ``seed``.
    """
    manifest = manifest if manifest is not None else empty_manifest()
    if not isinstance(source, str) or not source.strip():
        raise ScriptError(0, "the script is empty")
    if len(source) > MAX_SOURCE_CHARS:
        raise ScriptError(0, "the script is %d characters; the limit is %d" % (len(source), MAX_SOURCE_CHARS))
    if isinstance(seed, bool) or not isinstance(seed, int):
        seed = 0
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ScriptError(exc.lineno or 0, "syntax error: %s" % exc.msg) from None
    except (RecursionError, MemoryError, ValueError, OverflowError):
        raise ScriptError(0, "the script is malformed or nested too deeply to parse") from None
    interp = _Interp(manifest, seed)
    interp.probe = probe
    try:
        _check_tree(tree)
        _check_top_level(tree)
        for node in tree.body:
            interp.exec_statement(node)
        build = interp.funcs.get("build")
        interp.line = build.line
        interp.call_user(build, ast.Call(func=ast.Name(id="build"), args=[], keywords=[], lineno=build.line))
        document = {"version": opschema.VERSION, "kit": manifest.kit_module or "", "seed": seed,
                    "ops": interp.ops, "log": interp.log}
        text = json.dumps(document, separators=(",", ":"))
        if len(text) > opschema.MAX_JSON_BYTES:
            raise ScriptError(interp.line, "the recorded calls are larger than %d MB" % (opschema.MAX_JSON_BYTES >> 20))
        try:
            opschema.validate_ops(document, manifest.functions)
        except opschema.OpsError as exc:
            raise ScriptError(interp.line, "invalid recording: %s" % exc) from None
        return Recording(document, text)
    except ScriptError:
        raise
    except _Control:
        raise ScriptError(interp.line, "'break', 'continue' or 'return' used outside a function or loop") from None
    except RecursionError:
        raise ScriptError(interp.line, "the script is nested too deeply") from None
    except MemoryError:
        raise ScriptError(interp.line, "the script used too much memory") from None
    except Exception as exc:  # noqa: BLE001 - the contract is that only ScriptError escapes; the tests flag these
        raise ScriptError(interp.line, "internal error in the recorder (%s)" % exc.__class__.__name__) from None


__all__ = ["MAX_SOURCE_CHARS", "Recording", "ScriptError", "compile_script"]
