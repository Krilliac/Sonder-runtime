"""The closed value domain of the recorder (soundness property P1).

An interpreter value is None, bool, int (|x| < 2**63), a finite float, str (capped), list, tuple
or dict[str, ...] of such values (capped), a ``Handle`` (the opaque result of a recorded op) or a
``Pred`` (a serialised predicate).  Never a function, module, type, frame, generator, exception or
bpy object.  ``is_domain`` states the property so tests can check it on everything the interpreter
touched.
"""
from __future__ import annotations

import math

MAX_INT = 2 ** 63
MAX_STR = 100_000
MAX_CONTAINER = 200_000
MAX_DEPTH = 64


class Handle:
    """The opaque result of one recorded op.  Carries only an id and the name of the op that made it."""

    __slots__ = ("id", "origin")

    def __init__(self, ident: int, origin: str):
        self.id = ident
        self.origin = origin

    def __repr__(self) -> str:
        return "<result of %s>" % self.origin


class Pred:
    """A serialised predicate tree (see ``predicates``); only ever produced from a lambda in a predicate position."""

    __slots__ = ("tree",)

    def __init__(self, tree):
        self.tree = tree


def is_domain(value, depth: int = 0) -> bool:
    """True when ``value`` belongs to the closed value domain.

    The domain is the finite trees and DAGs of the types above.  A container that contains itself (a cycle) is
    NOT in the domain; the interpreter refuses to create one.  Iterative, so nesting depth cannot exhaust the
    Python stack; ``depth`` is accepted for compatibility and ignored.
    """
    active = set()
    done = set()
    stack = [(value, False)]
    while stack:
        item, leaving = stack.pop()
        if leaving:
            active.discard(id(item))
            done.add(id(item))
            continue
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, int):
            if not -MAX_INT < item < MAX_INT:
                return False
        elif isinstance(item, float):
            if not math.isfinite(item):
                return False
        elif isinstance(item, str):
            if len(item) > MAX_STR:
                return False
        elif isinstance(item, (Handle, Pred)):
            continue
        elif isinstance(item, (list, tuple, dict)):
            key = id(item)
            if key in active:
                return False
            if key in done:
                continue
            if len(item) > MAX_CONTAINER:
                return False
            active.add(key)
            stack.append((item, True))
            if isinstance(item, dict):
                if not all(isinstance(name, str) for name in item):
                    return False
                children = item.values()
            else:
                children = item
            for child in children:
                stack.append((child, False))
        else:
            return False
    return True


def copy_value(value, depth: int = 0):
    """A fresh copy of a domain value (constants handed out of the manifest must not be shared)."""
    if depth > MAX_DEPTH:
        raise ValueError("value nested too deeply")
    if isinstance(value, list):
        return [copy_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(copy_value(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return {key: copy_value(item, depth + 1) for key, item in value.items()}
    return value


__all__ = ["Handle", "MAX_CONTAINER", "MAX_DEPTH", "MAX_INT", "MAX_STR", "Pred", "copy_value", "is_domain"]
