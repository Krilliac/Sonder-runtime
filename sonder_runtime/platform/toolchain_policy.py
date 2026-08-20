"""Platform policy for safe, fixed-argument toolchain status probes.

The policy owns which discovered tools may be probed and which immutable
version arguments are permitted.  Process execution remains in the adapter
that enforces output and timeout bounds.
"""
from __future__ import annotations

import sonder_runtime.platform.environment_probe as environment_probe
from sonder_runtime.platform.logging import Redactor


VERSION_ARGUMENTS = {
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


def safe_output(text: str, *, max_chars: int = 2_000) -> str:
    """Normalize and redact bounded output from a fixed toolchain probe."""
    value = Redactor().redact((text or "").strip())
    if len(value) > max_chars:
        return value[:max_chars] + "\n[output truncated]"
    return value


def normalized_tool_name(name: str) -> str:
    """Normalize a caller-supplied tool name before policy lookup."""
    return (name or "").strip().lower()


def allowed_arguments(name: str) -> tuple[str, ...] | None:
    """Return the only arguments allowed for a supported tool."""
    return VERSION_ARGUMENTS.get(normalized_tool_name(name))


def discovered_path(name: str, *, refresh: bool = False) -> str:
    """Resolve a supported tool only from the canonical environment profile."""
    tool = normalized_tool_name(name)
    env = environment_probe.probe(refresh=refresh)
    return (
        env.get("toolchains", {}).get(tool)
        or env.get("specialist_tools", {}).get(tool)
        or ""
    )


__all__ = [
    "VERSION_ARGUMENTS",
    "allowed_arguments",
    "discovered_path",
    "normalized_tool_name",
    "safe_output",
]
