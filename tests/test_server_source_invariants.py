"""Structural invariants over server.py that no test can express behaviourally.

A duplicate module-level ``def`` is not a syntax error and not a runtime error.
Python simply binds the name twice and the *last* one wins, so the earlier
definition becomes unreachable code that still reads like the live one. Every
call site resolves to the survivor at call time, including call sites written
above it.

That is worth an invariant rather than a code review habit, because of what it
does to a fix: someone tracing a bug to the first definition, editing it, and
watching the tests stay green has changed nothing at all. A guard that silently
no-ops is the defect class this branch exists to close, and a shadowed function
is that defect with the shadowing done by the language itself.

There is no ruff/flake8 configuration in this repo, so F811 is not caught for
us anywhere else.
"""
from __future__ import annotations

import ast
import pathlib

import server


def _server_source():
    return pathlib.Path(server.__file__).read_text(encoding="utf-8")


def test_no_module_level_function_in_server_is_defined_twice():
    """The second definition wins silently; the first is a trap for a fixer."""
    tree = ast.parse(_server_source())
    seen: dict[str, int] = {}
    duplicates = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in seen:
            duplicates.append(
                "%s: line %d is dead, shadowed by the definition at line %d"
                % (node.name, seen[node.name], node.lineno)
            )
        seen[node.name] = node.lineno

    assert duplicates == [], "\n".join(duplicates)
    # A sentinel, so an ast change that stopped finding functions at all could
    # not turn this into a test that passes by inspecting nothing. Measured at
    # 449 module-level functions when this was written; the floor is set well
    # below that so ordinary growth or pruning does not trip it, and well above
    # zero so an empty walk does.
    assert len(seen) > 400, "expected server.py to define many functions, saw %d" % (
        len(seen),
    )
