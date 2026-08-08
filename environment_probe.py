"""environment_probe -- deterministic discovery of the host the runtime is on.

A local model asked to run commands guesses its platform -- it will emit
`ls`/`grep` on Windows or `dir` on Linux and burn agent steps on the failures.
This module answers the question once, deterministically: which OS, which
shells exist, which interpreters and toolchains are installed, and which
run_code languages will actually work here. The workbench agent injects one
compact line of this into every run so the model is environment-reactive
without spending a tool call; the full profile is exposed as a read-only tool
and the /env REPL command.

Everything here is a filesystem/platform lookup (shutil.which, platform.*) --
no subprocesses are spawned, so probing is fast and cannot hang on a wedged
binary. Version strings are therefore not collected; presence and path are
what an agent needs to pick the right command shape.

Stdlib only.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile

# Executables worth knowing about, grouped by what an agent uses them for.
_SHELLS = ("powershell", "pwsh", "cmd", "bash", "sh", "zsh", "wsl")
_TOOLCHAINS = (
    "python", "python3", "node", "npm", "npx", "tsc", "deno",
    "cargo", "rustc", "go", "dotnet", "msbuild", "java", "javac", "mvn",
    "gradle", "cmake", "ninja", "make", "gcc", "g++", "clang", "clang++",
    "cl", "git", "gh", "docker", "kubectl", "rg", "curl", "wget", "tar",
    "7z", "unzip", "pip", "uv", "ruff", "pytest",
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
    """The host environment as one cached dict. `refresh=True` re-probes.

    Keys: os, os_release, machine, is_windows/is_linux/is_mac, python_version,
    python_executable, cwd, temp_dir, path_separator, cpu_count, shells
    {name: path}, toolchains {name: path}, preferred_shell.
    """
    global _cache
    if _cache is not None and not refresh:
        return _cache

    system = platform.system()
    is_windows = system == "Windows"
    shells = _which_map(_SHELLS)

    # The shell an agent should reach for first on this host: PowerShell on
    # Windows (cmd is the fallback), bash elsewhere (sh is the fallback).
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
        "preferred_shell": preferred,
    }
    return _cache


def agent_brief(refresh=False):
    """One compact line for the agent prompt: platform, shells, key tools.

    Kept to a single line on purpose -- it is injected into EVERY agent run,
    so it must inform the command-shape choice (Windows vs POSIX, which
    build tool exists) without spending meaningful context.
    """
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
    """The full profile as readable text for /env and the status tool."""
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
    return "\n".join(lines)
