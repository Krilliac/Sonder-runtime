"""The permission story the Flutter app tells, against the one the gate enforces.

Two surfaces carry a permission claim to the operator, and they are fed by two
different vocabularies:

* ``GET /v1/permission-mode`` ships ``permission_modes._MATRIX[active]`` --
  risk class -> allow | ask | deny -- straight from the enforcing module. Five
  classes: safe, ask, mutation, execution, dangerous.
* ``GET /v1/commands`` ships a per-command ``risk`` from
  ``command_catalog._risk_for``, which the app renders as a coloured dot with a
  behavioural tooltip.

Those two only agree if the per-command ``risk`` is keyed the same way the
matrix is. It was not: ``_risk_for`` never emitted ``execution``, the class
``permission_modes.risk_of`` splits out for tools that start a host process. So
``/build_run``, ``/test_run``, ``/typecheck_run`` and ``/dependency_audit``
reached the app as ``safe`` and were drawn with the green dot and the tooltip
"Safe -- read only", while the gate denied all four under ``plan`` and prompted
for them under ``manual`` and ``acceptEdits``. 27 of 273 commands disagreed.

The app is not the enforcement point and never decides -- these are claims made
to a person, not a bypass. But the person is who ``plan`` mode is for, and a
green "read only" dot on a tool that launches a compiler is a confident wrong
answer, which is the failure this repo's gate work keeps naming.

Both tests below are drift guards on a single source rather than a sync step:
the first pins the wire field to ``permission_modes.risk_of`` itself, the
second pins the app's rendering table to the keys of ``_MATRIX``. Adding a
sixth risk class breaks the second until the app is taught to draw it.
"""
import re
from pathlib import Path

import command_catalog
import permission_modes as pm


ROOT = Path(__file__).resolve().parents[1]


def _dart(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def _matrix_risk_classes():
    seen = set()
    for mode in pm.MODES:
        seen |= set(pm._MATRIX[mode])
    return seen


def _dart_switch_cases(source, function_name):
    """The `case '<literal>':` labels inside one Dart function body.

    Brace-matched from the signature rather than regexed over the whole file,
    so a case belonging to a neighbouring switch cannot be counted as this
    function's and let a missing branch pass.
    """
    start = source.index(function_name + "(")
    open_brace = source.index("{", start)
    depth = 0
    for index in range(open_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                body = source[open_brace:index]
                break
    else:  # pragma: no cover - unbalanced source is a syntax error upstream
        raise AssertionError("unbalanced braces after %s" % function_name)
    return set(re.findall(r"case\s+'([^']+)'\s*:", body))


def test_published_command_risk_is_the_class_the_gate_decides_on():
    """One vocabulary, or the dot and the matrix cannot be read together.

    The app receives ``risk`` per command and ``matrix`` per mode. The only
    way it can resolve "what will this command do right now" without keeping a
    policy of its own is for ``risk`` to be a key of ``matrix`` chosen by the
    same function the gate uses.
    """
    mismatched = [
        (command.name, command.risk, pm.risk_of(command.name))
        for command in command_catalog.catalog()
        if command.risk != pm.risk_of(command.name)
    ]
    assert not mismatched, (
        "these commands are published to the app under a different risk class "
        "than the gate decides on, so the app's risk dot contradicts the mode "
        "chip beside it: %s"
        % ", ".join(
            "%s published=%s gate=%s" % row for row in sorted(mismatched)
        )
    )


def test_every_published_risk_class_is_a_key_of_the_matrix():
    """A dot the matrix cannot key is a dot the app cannot explain."""
    classes = _matrix_risk_classes()
    stray = sorted(
        {command.risk for command in command_catalog.catalog()} - classes
    )
    assert not stray, (
        "published risk classes with no row in permission_modes._MATRIX: %s"
        % ", ".join(stray)
    )


def test_flutter_app_draws_every_risk_class_the_matrix_defines():
    """The app's rendering table, pinned to the enforcing module's keys.

    Both halves matter. ``_riskColor`` decides the colour and ``_riskLabel``
    decides the words; a class missing from either is a class the operator
    reads wrongly, and they are separate switch statements that can drift
    apart independently.
    """
    source = _dart("app/lib/chat_screen.dart")
    classes = _matrix_risk_classes()

    for function_name in ("_riskColor", "_riskLabel"):
        missing = sorted(classes - _dart_switch_cases(source, function_name))
        assert not missing, (
            "app/lib/chat_screen.dart %s has no branch for these risk classes, "
            "so the app falls back to its neutral 'risk not published' "
            "rendering for commands the gate does classify: %s"
            % (function_name, ", ".join(missing))
        )
