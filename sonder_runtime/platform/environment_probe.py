"""Deterministic, read-only discovery of the host runtime environment.

This is platform ownership for the environment profile consumed by workbench
and toolchain callers. Discovery only checks host metadata and executable
presence; it never starts a process or probes executable versions.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile

_SHELLS = ("powershell", "pwsh", "cmd", "bash", "sh", "zsh", "wsl")
_TOOLCHAINS = (
    "python", "python3", "node", "npm", "npx", "tsc", "deno",
    "cargo", "rustc", "go", "dotnet", "msbuild", "java", "javac", "mvn",
    "gradle", "cmake", "ninja", "make", "gcc", "g++", "clang", "clang++",
    "cl", "git", "gh", "docker", "kubectl", "rg", "curl", "wget", "tar",
    "7z", "unzip", "pip", "uv", "ruff", "pytest",
)
_SPECIALIST_TOOLS = (
    "sccache", "clcache", "doxygen", "xperf", "wpaexporter", "nssm",
)
_cache = None


def _which_map(names):
    found = {}
    for name in names:
        path = shutil.which(name)
        if path:
            found[name] = path
    return found


def probe(refresh=False):
    """Return the cached host profile, or refresh it when requested."""
    global _cache
    if _cache is not None and not refresh:
        return _cache

    system = platform.system()
    is_windows = system == "Windows"
    shells = _which_map(_SHELLS)
    if is_windows:
        preferred = next((s for s in ("pwsh", "powershell", "cmd") if s in shells), "")
    else:
        preferred = next((s for s in ("bash", "zsh", "sh") if s in shells), "")

    _cache = {
        "os": system,
        "os_release": platform.release(),
        "machine": platform.machine(),
        "is_windows": is_windows,
        "is_linux": system == "Linux",
        "is_mac": system == "Darwin",
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "temp_dir": tempfile.gettempdir(),
        "path_separator": os.sep,
        "cpu_count": os.cpu_count() or 1,
        "shells": shells,
        "toolchains": _which_map(_TOOLCHAINS),
        "specialist_tools": _which_map(_SPECIALIST_TOOLS),
        "preferred_shell": preferred,
    }
    return _cache


def agent_brief(refresh=False):
    """Return the compact, single-line environment summary for agents."""
    env = probe(refresh)
    tools = sorted(env["toolchains"])
    return (
        "environment: %s %s (%s) | preferred shell: %s | shells: %s | "
        "tools: %s | python %s | %d cpus"
        % (
            env["os"], env["os_release"], env["machine"],
            env["preferred_shell"] or "(none found)",
            ",".join(sorted(env["shells"])) or "(none)",
            ",".join(tools) or "(none)",
            env["python_version"], env["cpu_count"],
        )
    )


def format_profile(refresh=False):
    """Return the full host profile as readable text."""
    env = probe(refresh)
    lines = [
        "host environment",
        "  os: %s %s (%s)" % (env["os"], env["os_release"], env["machine"]),
        "  python: %s (%s)" % (env["python_version"], env["python_executable"]),
        "  cwd: %s" % env["cwd"],
        "  temp: %s" % env["temp_dir"],
        "  cpus: %d | path separator: %r" % (env["cpu_count"], env["path_separator"]),
        "  preferred shell: %s" % (env["preferred_shell"] or "(none found)"),
        "  shells:",
    ]
    for name in sorted(env["shells"]):
        lines.append("    %-12s %s" % (name, env["shells"][name]))
    if not env["shells"]:
        lines.append("    (none found)")
    lines.append("  toolchains:")
    for name in sorted(env["toolchains"]):
        lines.append("    %-12s %s" % (name, env["toolchains"][name]))
    if not env["toolchains"]:
        lines.append("    (none found)")
    lines.append("  specialist tools:")
    for name in sorted(env["specialist_tools"]):
        lines.append("    %-12s %s" % (name, env["specialist_tools"][name]))
    if not env["specialist_tools"]:
        lines.append("    (none found)")
    return "\n".join(lines)


__all__ = ["agent_brief", "format_profile", "probe"]
