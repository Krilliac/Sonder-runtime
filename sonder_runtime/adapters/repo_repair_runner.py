"""Bounded pytest run for one scratch repo-repair project.

The verdict distinguishes a candidate's own failure from infrastructure that
said nothing about it (a timeout, a failed spawn, pytest itself breaking) so
the reward store never banks a negative the model did not earn. It spawns a
child process, so it lives with the adapters. Moved from ``server.py`` in
the WP1 Three-Hundred-Twenty-Eighth Slice with its behaviour byte-for-byte
intact.
"""
from __future__ import annotations

import sys

from sonder_runtime.platform import logging as sonder_logging


def run_pytest(workdir, timeout):
    """Run pytest for one scratch project; (ok, bounded output, infra_error).

    ``infra_error`` is non-empty only when the verdict says nothing about the
    candidate code: a timeout, a failed spawn, or pytest itself breaking.
    Recording those as model failures poisons the reward store with negative
    signals the model did not earn (observed 2026-08-02: a memory-starved
    20-job run banked 20 bogus ``failed`` outcomes). The converse matters just
    as much - a candidate that fails to even import is the model's fault and
    must stay attributable.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", "-x"],
            cwd=str(workdir), capture_output=True, text=True,
            # This server's own stdin is the MCP protocol pipe. A child that
            # inherits it can block forever on a read nobody will answer —
            # exactly how every job in a 20-job run timed out while the
            # identical call from a child process took 0.8s.
            stdin=subprocess.DEVNULL,
            timeout=max(5, timeout),
            env=sonder_logging.child_environment(),
        )
    except subprocess.TimeoutExpired:
        return False, "pytest timed out", "pytest timed out"
    except OSError as exc:
        return False, str(exc)[:200], "pytest could not start: %s" % (
            type(exc).__name__,
        )
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # pytest exit codes: 0 passed, 1 test failed, 2 interrupted (a collection
    # error), 3 internal error, 4 usage error, 5 no tests collected.
    #
    # 2 is the candidate's own fault and must stay attributable. A module the
    # model wrote with a SyntaxError fails at import, pytest aborts collection,
    # and the error is reported against the test file that imported it -
    # observed live when a generation leaked activity-log text into module.py.
    # Treating 2 as infrastructure excused a real model failure, which is the
    # mirror image of the bug this split was added to fix.
    #
    # 3/4/5 say nothing about the candidate: pytest broke, was misinvoked, or
    # the test file never arrived. Those stay unattributable, as do a timeout
    # and a failed spawn above.
    infra = ""
    if proc.returncode not in (0, 1, 2):
        infra = "pytest exited %d without a test verdict" % proc.returncode
    return proc.returncode == 0, output[-1500:], infra
