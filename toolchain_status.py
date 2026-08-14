"""Bounded version/status probes for already-discovered host tools.

This is deliberately *not* a command runner.  A caller may name only a tool
that :mod:`environment_probe` found on PATH, and every supported tool has one
fixed, argument-free status invocation.  That gives an agent a grounded way
to turn discovery into evidence without accepting a shell, executable path,
or caller-controlled arguments.
"""
from __future__ import annotations

import subprocess

import environment_probe
import sonder_logging


TIMEOUT_SECONDS = 3
MAX_OUTPUT_CHARS = 2_000

# Keep this intentionally small.  A tool must have a non-interactive,
# read-only version switch before it can be probed.  Unknown tools remain
# discoverable through environment_status but are not executable here.
_VERSION_ARGUMENTS = {
    "python": ("--version",),
    "python3": ("--version",),
    "node": ("--version",),
    "npm": ("--version",),
    "npx": ("--version",),
    "cargo": ("--version",),
    "rustc": ("--version",),
    "go": ("version",),
    "dotnet": ("--version",),
    "cmake": ("--version",),
    "ninja": ("--version",),
    "gcc": ("--version",),
    "g++": ("--version",),
    "clang": ("--version",),
    "clang++": ("--version",),
    "git": ("--version",),
    "gh": ("--version",),
    "rg": ("--version",),
    "curl": ("--version",),
    "pip": ("--version",),
    "uv": ("--version",),
    "ruff": ("--version",),
    "pytest": ("--version",),
    "sccache": ("--version",),
    "clcache": ("--version",),
    "doxygen": ("--version",),
}


def _available_path(name: str, refresh: bool) -> str:
    env = environment_probe.probe(refresh=refresh)
    return (env.get("toolchains", {}).get(name)
            or env.get("specialist_tools", {}).get(name)
            or "")


def _safe_output(completed: subprocess.CompletedProcess[str]) -> str:
    # Version commands should be tiny, but retain a hard presentation bound and
    # redact any accidental credentials emitted by a local wrapper.
    text = (completed.stdout or completed.stderr or "").strip()
    text = sonder_logging.Redactor().redact(text)
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + "\n[output truncated]"
    return text


def status(name: str, refresh: bool = False) -> dict[str, object]:
    """Run a fixed non-interactive version probe for one discovered tool."""
    tool = (name or "").strip().lower()
    if tool not in _VERSION_ARGUMENTS:
        return {
            "ok": False,
            "tool": tool,
            "error": "unsupported tool; use environment_status to inspect supported host tools",
        }
    path = _available_path(tool, refresh)
    if not path:
        return {"ok": False, "tool": tool, "error": "tool is not available on this host"}
    try:
        completed = subprocess.run(
            [path, *_VERSION_ARGUMENTS[tool]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=TIMEOUT_SECONDS,
            shell=False,
            env=sonder_logging.child_environment(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "tool": tool, "error": "status probe timed out"}
    except OSError:
        return {"ok": False, "tool": tool, "error": "status probe could not start"}
    output = _safe_output(completed)
    if completed.returncode != 0:
        # A broken or wrapped executable controls stderr.  Do not copy its
        # arbitrary failure text into activity history; the exit verdict is
        # enough for an agent to choose a different safe path.
        return {"ok": False, "tool": tool, "error": "status probe failed"}
    return {"ok": True, "tool": tool, "output": output}
