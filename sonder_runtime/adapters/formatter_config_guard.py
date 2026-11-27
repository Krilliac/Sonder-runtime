"""Run a protected format check without loading anything the repository controls.

``format_code(check_only=True)`` is a read-only tool, yet every formatter it can
start reads project files that decide what code runs: ``npx --no-install
prettier`` resolves ``node_modules/.bin/prettier`` and ``.npmrc`` from the
working folder and every parent, and Prettier loads ``prettier.config.js``,
``plugins`` and a module-path ``parser`` from configs it finds above each file.
Inspecting those files failed review four times (each round found another way
in), so the check path no longer reads project configuration at all:

- Prettier: never ``npx``. :func:`resolve_trusted_prettier` returns an absolute
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


def _trusted_file(path: str, roots: Sequence[Path]) -> bool:
    return os.path.isabs(path) and os.path.isfile(path) and not _forbidden(path, roots)


def trusted_which(
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
    :func:`_trusted_file`.
    """
    source = os.environ if environ is None else environ
    path_value = next((value for key, value in source.items() if key.upper() == "PATH"), "")
    for entry in path_value.split(os.pathsep):
        if _entry_forbidden(entry, forbidden_roots):
            continue
        found = which_in_directory(name, entry)
        if found and _trusted_file(found, forbidden_roots):
            return found
    return None


def _runtime_module_outside(name: str, forbidden_roots: Sequence[Path]) -> bool:
    """True when the runtime interpreter's own absolute ``sys.path`` provides ``name`` outside every forbidden root.

    Looked up, not imported. Relative entries (``''`` is the current folder) are
    skipped: the child runs ``python -I``, which has none. The first absolute
    provider decides, so a copy inside a forbidden root that shadows a trusted
    one refuses the module route.
    """
    entries = [entry for entry in sys.path if isinstance(entry, str) and os.path.isabs(entry)]
    try:
        spec = importlib.machinery.PathFinder.find_spec(name, entries)
    except (ImportError, ValueError, OSError):
        return False
    if spec is None:
        return False
    locations = [spec.origin] if spec.origin and spec.origin not in ("built-in", "frozen") else []
    locations += list(spec.submodule_search_locations or [])
    if not locations:
        return False
    return not any(_forbidden(location, forbidden_roots) for location in locations)


def pip_audit_command(forbidden_roots: Sequence[Path]) -> tuple[list[str] | None, str | None]:
    """``(argv prefix, None)`` that runs pip-audit without project code, or ``(None, error)``.

    The runtime interpreter with ``-I -m pip_audit`` when it provides the module
    outside every forbidden root; else an absolute PATH executable found by
    :func:`trusted_which`.
    """
    if _runtime_module_outside("pip_audit", forbidden_roots):
        return [sys.executable, "-I", "-m", "pip_audit"], None
    found = trusted_which("pip-audit", forbidden_roots)
    if found:
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


def resolve_trusted_prettier(
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
        if _trusted_file(configured, forbidden_roots):
            return configured, None
        return None, (
            "%s must be the absolute path of a Prettier executable outside the project and every "
            "authorized root" % PRETTIER_SETTING)
    found = trusted_which("prettier", forbidden_roots, source)
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
    """``base`` without ``NODE_*``/``npm_config_*``/``PRETTIER_*`` keys and PATH entries in a forbidden root.

    Keys compare case-insensitively (Windows environment names are). A relative
    PATH entry is dropped too: it resolves against the child's working folder.
    """
    env: dict[str, str] = {}
    for key, value in base.items():
        if key.upper().startswith(_NODE_ENV_PREFIXES):
            continue
        if key.upper() == "PATH":
            kept = [
                entry for entry in value.split(os.pathsep)
                if not _entry_forbidden(entry, forbidden_roots)
            ]
            value = os.pathsep.join(kept)
        env[key] = value
    return env


def runtime_module_available(name: str) -> bool:
    """True when the runtime interpreter can import ``name`` (looked up, not imported)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def python_formatter_command(tool: str, forbidden_roots: Sequence[Path]) -> tuple[list[str] | None, str | None]:
    """``(argv prefix, None)`` that runs ``tool`` without a project interpreter, or ``(None, error)``.

    The runtime's own interpreter with ``-I`` (no cwd or user site on ``sys.path``)
    when it has the module; else an absolute PATH executable outside every
    forbidden root. ``tool`` is ``"ruff"`` or ``"black"``.
    """
    if runtime_module_available(tool):
        return [sys.executable, "-I", "-m", tool], None
    found = trusted_which(tool, forbidden_roots)
    if found:
        return [found], None
    return None, (
        "refusing to run %s in check mode: it is not installed in the Sonder runtime and the one on "
        "PATH (if any) is inside the project or an authorized root" % tool)
