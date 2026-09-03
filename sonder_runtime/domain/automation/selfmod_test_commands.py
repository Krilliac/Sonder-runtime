"""Selfmod test command construction for automated code modification runs.
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def selfmod_test_commands(run, explicit_tests):
    import shlex
    workspace = Path(run["workspace_path"])
    declared_python = sorted(path for path in run["files"] if path.lower().endswith(".py"))
    present_python = [path for path in declared_python if (workspace / path).is_file()]
    absent_python = [path for path in declared_python if not (workspace / path).is_file()]
    if present_python:
        syntax = [sys.executable, "-m", "py_compile", *present_python]
    elif declared_python:
        # `.is_file()` used to empty this list silently and the required syntax
        # check degraded to `print('no Python syntax targets')` -- exit 0,
        # recorded as passing. The list empties exactly when the candidate
        # DELETED its declared modules, and deletion is the shape an automated
        # repair loop is most likely to produce, so the one change that most
        # needed a syntax gate was the one that skipped it.
        syntax = [sys.executable, "-c", "raise SystemExit(%r)" % (
            "selfmod syntax gate: every declared Python target is absent from the "
            "candidate workspace (%s). A deletion-only change has nothing to compile "
            "in place, and an empty target set is a refusal, not a pass. Re-scope the "
            "run so a surviving module carries the change, or take the deletion "
            "through an explicit maintenance review."
            % ", ".join(absent_python)
        )]
    else:
        syntax = [sys.executable, "-c", "raise SystemExit(%r)" % (
            "selfmod syntax gate: this run declares no Python file (%s), so the "
            "required syntax check has nothing to compile. An empty target set is a "
            "refusal, not a pass." % (", ".join(run["files"]) or "no files")
        )]
    targeted = shlex.split(explicit_tests[0], posix=os.name != "nt") if explicit_tests else [sys.executable, "-c", "raise SystemExit('explicit reproducing/targeted test required')"]
    regression = [sys.executable, "-m", "pytest", "-q"]
    # `smoke` is deliberately NOT built here. It used to be
    #     python -c "import pathlib; assert pathlib.Path('.').is_dir(); ..."
    # run with the candidate workspace as cwd -- a required gate that could not
    # fail. It is now selfmod.record_smoke(), which imports the candidate in a
    # child process and must return a SHA-256 receipt over the bytes it loaded.
    # It lives in selfmod.py because the receipt has to be computed by the
    # recording process from the workspace, and never handed to the probe.
    commands = [("syntax", syntax), ("targeted", targeted), ("regression", regression)]
    if run["maintenance_authorized"]:
        security = shlex.split(explicit_tests[1], posix=os.name != "nt") if len(explicit_tests) > 1 else [sys.executable, "-c", "raise SystemExit('explicit protected security test required')"]
        commands.append(("security", security))
    return commands
