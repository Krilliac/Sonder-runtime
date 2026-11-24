"""Bounded version/status probes for already-discovered host tools.

This is deliberately *not* a command runner.  A caller may name only a tool
that :mod:`environment_probe` found on PATH, and every supported tool has one
fixed, argument-free status invocation.  That gives an agent a grounded way
to turn discovery into evidence without accepting a shell, executable path,
or caller-controlled arguments.
"""
from __future__ import annotations

import os
import signal
import subprocess

import sonder_logging
from sonder_runtime.adapters import process_termination
from sonder_runtime.adapters.host_tools import bounded_process
from sonder_runtime.platform import toolchain_policy


TIMEOUT_SECONDS = 3
MAX_OUTPUT_CHARS = 2_000

# Keep this intentionally small.  A tool must have a non-interactive,
# read-only version switch before it can be probed.  Unknown tools remain
# discoverable through environment_status but are not executable here.
def _available_path(name: str, refresh: bool) -> str:
    return toolchain_policy.discovered_path(name, refresh=refresh)


def _safe_output(text: str) -> str:
    """Compatibility delegate for packaged toolchain-output policy."""
    return toolchain_policy.safe_output(text, max_chars=MAX_OUTPUT_CHARS)


def _terminate_process_tree(proc) -> None:
    """Compatibility delegate for the packaged process-teardown adapter."""
    return process_termination.terminate_process_tree(
        proc,
        os_module=os,
        signal_module=signal,
        subprocess_module=subprocess,
    )


def _run_bounded(argv: list[str]) -> tuple[str, str]:
    """Run fixed argv through the packaged bounded runner.

    The packaged adapter owns launch, drain and process-tree termination.
    This wrapper reads its limits and the ``subprocess``/``os`` modules from
    THIS module at call time so existing monkeypatch seams keep working.
    """
    result = bounded_process.run_bounded(
        argv,
        timeout_seconds=TIMEOUT_SECONDS,
        max_output_chars=MAX_OUTPUT_CHARS,
        env=sonder_logging.child_environment(),
        subprocess_module=subprocess,
        os_module=os,
    )
    if result.outcome == "start_failed":
        raise OSError("status probe could not start")
    return result.outcome, result.output


def status(name: str, refresh: bool = False) -> dict[str, object]:
    """Run a fixed non-interactive version probe for one discovered tool."""
    tool = (name or "").strip().lower()
    arguments = toolchain_policy.allowed_arguments(tool)
    if arguments is None:
        return {
            "ok": False,
            "tool": tool,
            "error": "unsupported tool; use environment_status to inspect supported host tools",
        }
    path = _available_path(tool, refresh)
    if not path:
        return {"ok": False, "tool": tool, "error": "tool is not available on this host"}
    try:
        outcome, output = _run_bounded([path, *arguments])
    except subprocess.TimeoutExpired:
        return {"ok": False, "tool": tool, "error": "status probe timed out"}
    except OSError:
        return {"ok": False, "tool": tool, "error": "status probe could not start"}
    if outcome == "timeout":
        return {"ok": False, "tool": tool, "error": "status probe timed out"}
    if outcome == "output_limit":
        return {"ok": False, "tool": tool, "error": "status probe output exceeded limit"}
    if outcome != "ok":
        # A broken or wrapped executable controls stderr.  Do not copy its
        # arbitrary failure text into activity history; the exit verdict is
        # enough for an agent to choose a different safe path.
        return {"ok": False, "tool": tool, "error": "status probe failed"}
    return {"ok": True, "tool": tool, "output": _safe_output(output)}
