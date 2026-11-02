"""What the recorder knows about the kit: exposed names, signatures, literal constants, predicate parameters.

Built with ``ast`` from the kit SOURCE only (the kit is never imported or run on the host).
"""
from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field

from ..kit_policy import analyse_kit
from .values import MAX_CONTAINER, MAX_DEPTH, MAX_INT, MAX_STR


@dataclass(frozen=True)
class Signature:
    """Parameter names in order, how many are required, and whether extra positional/keyword arguments are accepted."""

    params: tuple
    required: int
    kwonly: tuple = ()
    varargs: bool = False
    varkw: bool = False

    @property
    def keywords(self) -> tuple:
        return self.params + self.kwonly


@dataclass(frozen=True)
class Manifest:
    kit_module: str | None
    exposed: frozenset = frozenset()
    signatures: dict = field(default_factory=dict)
    constants: dict = field(default_factory=dict)
    predicate_params: dict = field(default_factory=dict)

    @property
    def functions(self) -> frozenset:
        return frozenset(self.signatures)


def empty_manifest() -> Manifest:
    """A manifest for a run without a kit: core ops and the language only."""
    return Manifest(kit_module=None)


_BAD = object()


def _domain(value, depth=0):
    """``value`` as a recorder domain value, or ``_BAD`` when it is not one (sets, bytes, huge numbers ...)."""
    if depth > MAX_DEPTH:
        return _BAD
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _BAD
    if isinstance(value, int):
        return value if -MAX_INT < value < MAX_INT else _BAD
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else _BAD
    if isinstance(value, (list, tuple)) and len(value) <= MAX_CONTAINER:
        items = [_domain(item, depth + 1) for item in value]
        if any(item is _BAD for item in items):
            return _BAD
        return tuple(items) if isinstance(value, tuple) else items
    if isinstance(value, dict) and len(value) <= MAX_CONTAINER and all(isinstance(key, str) for key in value):
        out = {key: _domain(item, depth + 1) for key, item in value.items()}
        return _BAD if any(item is _BAD for item in out.values()) else out
    return _BAD


def _signature(node) -> Signature:
    if isinstance(node, ast.ClassDef):
        return Signature(params=(), required=0, varargs=True, varkw=True)
    args = node.args
    names = tuple(arg.arg for arg in list(args.posonlyargs) + list(args.args))
    required = len(names) - len(args.defaults)
    kwonly = tuple(arg.arg for arg in args.kwonlyargs)
    return Signature(params=names, required=required, kwonly=kwonly, varargs=args.vararg is not None,
                     varkw=args.kwarg is not None)


def _called_parameters(node) -> tuple:
    """Parameters of a kit function that its body calls (``predicate(p)``): those take lambdas from scripts."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return ()
    names = {arg.arg for arg in list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)}
    called = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in names:
            if sub.func.id not in called:
                called.append(sub.func.id)
    return tuple(called)


def _override(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return None
    return None


def build_manifest(kit_source: str, kit_module: str) -> Manifest:
    """Manifest for ``kit_source`` imported as ``kit_module``; an unparseable kit exposes nothing."""
    allowed, _excluded = analyse_kit(kit_source)
    try:
        tree = ast.parse(kit_source or "")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return Manifest(kit_module=kit_module)
    signatures: dict = {}
    constants: dict = {}
    predicates: dict = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in allowed:
            signatures[node.name] = _signature(node)
            called = _called_parameters(node)
            if called:
                predicates[node.name] = called
        elif isinstance(node, ast.Assign) or (isinstance(node, ast.AnnAssign) and node.value is not None):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in allowed:
                    try:
                        value = _domain(ast.literal_eval(node.value))
                    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
                        value = _BAD
                    if value is not _BAD:
                        constants[target.id] = value
    declared = _override(tree, "MESHGEN_PREDICATES")
    if isinstance(declared, dict):
        predicates = {str(key): tuple(str(item) for item in value) for key, value in declared.items()
                      if key in signatures and isinstance(value, (list, tuple))}
    return Manifest(kit_module=kit_module, exposed=frozenset(allowed), signatures=signatures,
                    constants=constants, predicate_params=predicates)


__all__ = ["Manifest", "Signature", "build_manifest", "empty_manifest"]
