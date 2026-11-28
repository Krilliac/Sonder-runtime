"""Run a protected format check without loading anything the repository controls.

``format_code(check_only=True)`` is a read-only tool, yet every formatter it can
start reads project files that decide what code runs: ``npx --no-install
prettier`` resolves ``node_modules/.bin/prettier`` and ``.npmrc`` from the
working folder and every parent, and Prettier loads ``prettier.config.js``,
``plugins`` and a module-path ``parser`` from configs it finds above each file.
Inspecting those files failed review four times (each round found another way
in), so the check path no longer reads project configuration at all:

- Prettier: never ``npx``. :func:`resolve_vetted_prettier` returns an absolute
  executable from ``SONDER_PRETTIER_EXE`` or PATH that is not inside the
  project or any authorized root; the caller confirms ``--version`` reports 3
  or newer (Prettier 2 loaded plugins and parsers on its own) and runs
  :func:`prettier_check_argv`: ``--no-config --no-editorconfig`` and an empty
  ``--ignore-path`` outside the root, from a working folder outside the root,
  with :func:`sanitized_node_environment` (no ``NODE_*``/``npm_config_*``, no
  PATH entry inside a forbidden root).
- Ruff and Black: :func:`python_formatter_command` runs them from the runtime
  interpreter (``python -I -m``) or a trusted PATH executable, never from a
  project venv; the caller adds ``--isolated`` (Ruff) or an empty ``--config``
  (Black) so project configuration is ignored.
- Biome, ``dotnet format`` and ``cargo fmt`` are refused in check mode by
  :func:`executable_config_error`: Sonder has no way to run them that ignores
  project files which choose code to run.

Stateless; safe from any thread. Nothing here starts a process.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from pathlib import Path

from .filesystem import file_ops
from .host_tools.probes import which_in_directory

PRETTIER_SETTING = "SONDER_PRETTIER_EXE"
MIN_PRETTIER_MAJOR = 3

_REFUSED_TOOLS = {
    "dotnet": "dotnet format evaluates the project through MSBuild (Directory.Build.*, analyzers)",
    "rustfmt": "cargo fmt reads .cargo/config.toml aliases and rust-toolchain.toml overrides",
    "biome": "biome runs through npx (which resolves node_modules/.bin and .npmrc from every parent folder) "
             "and reads project biome.json files that name plugins and extended configs",
}
_GLOB_CHARS = frozenset("*?[]{}!")
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
# Node and npm read these from the environment: NODE_OPTIONS (--require), NODE_PATH (module
# lookup) and npm_config_* (any .npmrc setting, including script-shell and node-options).
# PRETTIER_* switches Prettier's own behaviour (for example the experimental CLI).
_NODE_ENV_PREFIXES = ("NODE_", "NPM_CONFIG_", "PRETTIER_")


def _inside_any(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or file_ops._is_inside(path, root) for root in roots)


def _unsafe_spelling(path: str) -> bool:
    r"""True for a device (``\\.\``), extended-length (``\\?\``) or UNC (``\\host``) spelling.

    These name a location through a second route (a share on this machine, a
    device namespace) that the root comparison does not see, so they are refused
    outright instead of normalised.
    """
    if path.startswith("\\\\"):
        return True
    return os.name == "nt" and path.startswith("//")


def _forbidden(path: str, roots: Sequence[Path]) -> bool:
    """True when ``path`` (as spelled, or after resolving links) lies in one of ``roots``.

    Device, extended-length and UNC spellings are forbidden whatever they name.
    """
    if _unsafe_spelling(path):
        return True
    lexical = Path(os.path.normpath(path))
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return True
    return _inside_any(lexical, roots) or _inside_any(resolved, roots)


def _entry_forbidden(entry: str, roots: Sequence[Path]) -> bool:
    """True when the PATH ``entry`` is relative, oddly spelled, or (lexically or resolved) in ``roots``."""
    if not entry or not os.path.isabs(entry) or _unsafe_spelling(entry):
        return True
    lexical = Path(os.path.normpath(entry))
    try:
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return True
    return _inside_any(lexical, roots) or _inside_any(resolved, roots)


def _vetted_file(path: str, roots: Sequence[Path]) -> bool:
    return os.path.isabs(path) and os.path.isfile(path) and not _forbidden(path, roots)


def vetted_which(
    name: str,
    forbidden_roots: Sequence[Path],
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Absolute path of ``name`` found only in PATH folders outside every forbidden root, or None.

    Never ``shutil.which``: on Windows that searches the current directory first
    (unless ``NoDefaultCurrentDirectoryInExePath`` is set) and returns a relative
    result that CreateProcess then resolves against the parent's folder. Here a
    relative, device, extended-length, UNC or project-resident PATH entry is
    skipped, each remaining folder is searched on its own, and the hit must pass
    :func:`_vetted_file`.
    """
    source = os.environ if environ is None else environ
    path_value = next((value for key, value in source.items() if key.upper() == "PATH"), "")
    for entry in path_value.split(os.pathsep):
        if _entry_forbidden(entry, forbidden_roots):
            continue
        found = which_in_directory(name, entry)
        if found and _vetted_file(found, forbidden_roots):
            return found
    return None


def pip_audit_command(forbidden_roots: Sequence[Path]) -> tuple[list[str] | None, str | None]:
    """``(argv prefix, None)`` that runs pip-audit without project code, or ``(None, error)``.

    The runtime interpreter with ``-I -m pip_audit`` when its own install provides
    the module (:func:`runtime_module_available`); else an absolute, non-batch PATH
    executable found by :func:`vetted_which`.
    """
    if runtime_module_available("pip_audit"):
        return [sys.executable, "-I", "-m", "pip_audit"], None
    found = vetted_which("pip-audit", forbidden_roots)
    if found:
        refusal = batch_shim_error(found, "pip-audit")
        if refusal:
            return None, refusal
        return [found], None
    return None, (
        "no trusted pip-audit: install it in the Sonder runtime (pip install pip-audit) or outside the "
        "project and every authorized root; one on the current folder or inside the project is never run")


def executable_config_error(tool: str, root: Path | str, target: str = "") -> str | None:
    """Why a protected check of ``tool`` must not run here, or None.

    Refuses Biome, ``dotnet format`` and ``cargo fmt`` outright. For Prettier the
    target must be one existing path with no glob characters (Prettier expands a
    pattern itself).
    """
    prefix = "refusing to run %s in check mode: " % tool
    if tool in _REFUSED_TOOLS:
        return prefix + _REFUSED_TOOLS[tool]
    if tool != "prettier" or not target or target == ".":
        return None
    if any(ch in _GLOB_CHARS for ch in target):
        return prefix + "the path contains glob characters; the formatter would expand it past the checked folders"
    if not os.path.lexists(Path(root) / target):
        return prefix + "the path does not exist"
    return None


def resolve_vetted_prettier(
    forbidden_roots: Sequence[Path],
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """``(absolute executable, None)`` for a Prettier outside every forbidden root, or ``(None, error)``.

    ``SONDER_PRETTIER_EXE`` wins when set and must name an absolute file; it never
    falls back to PATH. Otherwise the PATH lookup must give an absolute path. A
    candidate inside a forbidden root (the project root and every authorized
    root), as spelled or after resolving links, is refused.
    """
    source = os.environ if environ is None else environ
    configured = source.get(PRETTIER_SETTING, "").strip()
    if configured:
        if _vetted_file(configured, forbidden_roots):
            return configured, None
        return None, (
            "%s must be the absolute path of a Prettier executable outside the project and every "
            "authorized root" % PRETTIER_SETTING)
    found = vetted_which("prettier", forbidden_roots, source)
    if found:
        return found, None
    return None, (
        "no trusted Prettier: install Prettier 3 outside the project and every authorized root and set "
        "%s to its absolute path (Sonder never runs npx, a project's node_modules, or a prettier found "
        "in the current folder or inside the project in check mode)" % PRETTIER_SETTING)


def prettier_major_version(stdout: str) -> int | None:
    """The major version from ``prettier --version`` output, or None when it is not a version."""
    lines = stdout.strip().splitlines()
    match = _VERSION.fullmatch(lines[0].strip()) if lines else None
    return int(match.group(1)) if match else None


def prettier_check_argv(executable: str, ignore_path: Path, target: Path) -> list[str]:
    """The check argv: no config, no editorconfig, an explicit ignore file, an absolute target."""
    return [
        executable, "--check", "--no-config", "--no-editorconfig",
        "--ignore-path", str(ignore_path), str(target),
    ]


def sanitized_node_environment(
    base: Mapping[str, str],
    forbidden_roots: Sequence[Path],
) -> dict[str, str]:
    """``base`` without ``NODE_*``/``npm_config_*``/``PRETTIER_*`` keys and PATH entries in a forbidden root, with the current-folder program search off (:func:`check_mode_environment`).

    Keys compare case-insensitively (Windows environment names are). A relative
    PATH entry is dropped too: it resolves against the child's working folder.
    """
    kept = {key: value for key, value in base.items() if not key.upper().startswith(_NODE_ENV_PREFIXES)}
    return check_mode_environment(kept, forbidden_roots)


def _runtime_install_dirs() -> list[str]:
    """The folders ``python -I`` searches for the running interpreter: stdlib, purelib and platlib."""
    paths = sysconfig.get_paths()
    seen: list[str] = []
    for key in ("purelib", "platlib", "stdlib", "platstdlib"):
        folder = paths.get(key)
        if folder and os.path.isabs(folder) and os.path.isdir(folder) and folder not in seen:
            seen.append(folder)
    return seen


def runtime_module_available(name: str) -> bool:
    """True when the running interpreter's own install provides ``name`` (looked up, not imported).

    The search path is the one ``python -I`` has: the interpreter's stdlib and
    site-packages from ``sysconfig``. ``PYTHONPATH``, ``''``, the script folder
    and every other ``sys.path`` entry are left out, so parent and child agree
    and a same-named module in the project (or any folder the process happens to
    have on ``sys.path``) never counts. The runtime's own install is trusted even
    when it lies under an authorized root (the runtime is not project code).
    """
    folders = _runtime_install_dirs()
    try:
        return bool(folders) and importlib.machinery.PathFinder.find_spec(name, folders) is not None
    except (ImportError, ValueError, OSError):
        return False


_BATCH_SUFFIXES = (".bat", ".cmd")


def batch_shim_error(executable: str, tool: str) -> str | None:
    """Why a protected check must not run the batch file ``executable``, or None.

    cmd.exe runs a ``.bat``/``.cmd`` and resolves every bare command inside it
    (``where``, ``pwsh``, ``node``...) from the current folder and the inherited
    PATH, so a shim outside the project still runs project files. A check-mode
    run starts from the project folder, so batch shims are refused outright.
    """
    if executable.rstrip(" .").lower().endswith(_BATCH_SUFFIXES):
        return (
            "refusing to run %s in check mode: the trusted one is a batch shim (%s), and cmd.exe would "
            "resolve the commands it calls from the project folder; install %s as a real executable"
            % (tool, os.path.basename(executable), tool))
    return None


def check_mode_environment(base: Mapping[str, str], forbidden_roots: Sequence[Path]) -> dict[str, str]:
    """``base`` for a protected check-mode child: PATH limited to trusted folders, cwd search off.

    PATH keeps only absolute entries outside every forbidden root (relative and
    empty entries resolve against the child's folder), and
    ``NoDefaultCurrentDirectoryInExePath=1`` stops cmd.exe and the Windows
    loader's program search from consulting the current folder.

    Variables that make an interpreter load code named by the environment are
    dropped: every ``PYTHON*`` key (a trusted console-script launcher starts
    its Python without ``-I``, so ``PYTHONPATH`` would import a project
    ``sitecustomize.py``), plus Node and Dart VM options. User site is off and
    safe-path is on for any Python child, and on Windows ``ComSpec`` is pinned
    to the system ``cmd.exe``.
    """
    env: dict[str, str] = {}
    for key, value in base.items():
        upper = key.upper()
        if upper in _CHECK_MODE_DROPPED_KEYS or upper.startswith("PYTHON"):
            continue
        if upper == "PATH":
            value = os.pathsep.join(
                entry for entry in value.split(os.pathsep) if not _entry_forbidden(entry, forbidden_roots))
        env[key] = value
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONSAFEPATH"] = "1"
    if os.name == "nt":
        system_root = next(
            (value for key, value in base.items() if key.upper() == "SYSTEMROOT"),
            os.environ.get("SystemRoot") or r"C:\Windows")
        env["ComSpec"] = str(Path(system_root) / "System32" / "cmd.exe")
    return env


_CHECK_MODE_DROPPED_KEYS = frozenset({
    "NODEFAULTCURRENTDIRECTORYINEXEPATH", "COMSPEC",
    "NODE_OPTIONS", "NODE_PATH", "DART_VM_OPTIONS",
})


def python_formatter_command(tool: str, forbidden_roots: Sequence[Path]) -> tuple[list[str] | None, str | None]:
    """``(argv prefix, None)`` that runs ``tool`` without a project interpreter, or ``(None, error)``.

    The runtime's own interpreter with ``-I`` (no cwd or user site on ``sys.path``)
    when it has the module; else an absolute PATH executable outside every
    forbidden root. ``tool`` is ``"ruff"`` or ``"black"``.
    """
    if runtime_module_available(tool):
        return [sys.executable, "-I", "-m", tool], None
    found = vetted_which(tool, forbidden_roots)
    if found:
        refusal = batch_shim_error(found, tool)
        if refusal:
            return None, refusal
        return [found], None
    return None, (
        "refusing to run %s in check mode: it is not installed in the Sonder runtime and the one on "
        "PATH (if any) is inside the project or an authorized root" % tool)
