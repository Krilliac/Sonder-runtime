"""Refuse project formatter configuration that executes code (protected check mode).

``prettier --check`` loads ``prettier.config.js`` (or ``.prettierrc.mjs``, a
``plugins`` entry in a data config, or a shared-config package named in
``package.json``/``package.yaml``) in the process that runs it, and Prettier
resolves a config for each file from that file's own folder. A "read-only"
check of a hostile repository is therefore code execution. Biome loads
plugins from ``biome.json``. ``format_code(check_only=True)`` calls
:func:`executable_config_error` before it starts a formatter. It refuses when:

- any directory under the checked path (every folder the formatter will visit,
  except ``node_modules`` and ``.git``) or above it up to the root carries an
  executable config, a data config that names plugins or a shared config, or a
  linked folder is present (a link could hide a config);
- the project ships its own formatter (``node_modules/.bin/<tool>`` or the
  package, in any checked folder): ``npx --no-install`` would run that
  committed code.

Data configs are parsed before their keys are inspected: JSON and TOML with the
standard parsers (so an escaped key such as ``"plug\\u0069ns"`` is seen as
``plugins``). YAML and JSON5 have no parser in the standard library, so they are
refused when the word ``plugins`` appears anywhere or the file contains a
backslash escape. Anything that cannot be read or parsed is refused.

Stateless; safe from any thread. Reads only small regular files and never
follows a link.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .filesystem import file_ops

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - older runtimes fall back to the text rule
    tomllib = None  # type: ignore[assignment]

_EXECUTABLE_PRETTIER = (
    "prettier.config.js", "prettier.config.cjs", "prettier.config.mjs",
    "prettier.config.ts", "prettier.config.cts", "prettier.config.mts",
    ".prettierrc.js", ".prettierrc.cjs", ".prettierrc.mjs",
    ".prettierrc.ts", ".prettierrc.cts", ".prettierrc.mts",
)
_DATA_PRETTIER = (
    ".prettierrc", ".prettierrc.json", ".prettierrc.json5", ".prettierrc.yaml",
    ".prettierrc.yml", ".prettierrc.toml",
)
_BIOME_CONFIGS = ("biome.json", "biome.jsonc", ".biome.json", ".biome.jsonc")
_PLUGINS_WORD = re.compile(r"plugins", re.IGNORECASE)
_PRETTIER_YAML_BLOCK = re.compile(r"^[\"']?prettier[\"']?[ \t]*:(.*(?:\n(?:[ \t].*)?)*)", re.MULTILINE)
_MAX_CONFIG_BYTES = 256 * 1024
_MAX_DIRECTORIES = 20000
_PRUNED = frozenset({"node_modules", ".git"})
_NODE_PACKAGES = {"prettier": "prettier", "biome": "@biomejs/biome"}
_CHECKED_TOOLS = frozenset(_NODE_PACKAGES)


def _read_text(path: Path) -> str | None:
    try:
        if file_ops.is_reparse_point(path) or path.stat().st_size > _MAX_CONFIG_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _has_plugins_key(value: object) -> bool:
    """True when a parsed document has a ``plugins`` key at any depth."""
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and key.lower() == "plugins") or _has_plugins_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_plugins_key(item) for item in value)
    return False


def _text_may_name_plugins(text: str) -> bool:
    """True when an unparsed (YAML/JSON5) document may name plugins."""
    return bool(_PLUGINS_WORD.search(text)) or "\\" in text


def _data_config_error(path: Path) -> str | None:
    text = _read_text(path)
    if text is None:
        return "%s is a link, is too large or could not be read" % path.name
    if "\x00" in text:
        return "%s is not a text file" % path.name
    suffix = path.suffix.lower()
    if suffix == ".toml" and tomllib is not None:
        try:
            parsed = tomllib.loads(text)
        except ValueError:
            return "%s could not be parsed" % path.name
        return "%s loads formatter plugins" % path.name if _has_plugins_key(parsed) else None
    if suffix in (".yaml", ".yml", ".json5", ".toml"):
        return "%s may load formatter plugins" % path.name if _text_may_name_plugins(text) else None
    try:
        parsed = json.loads(text)
    except ValueError:
        # An extension-less .prettierrc may be YAML; a .json file must be valid JSON.
        if path.name == ".prettierrc":
            return "%s may load formatter plugins" % path.name if _text_may_name_plugins(text) else None
        return "%s could not be parsed" % path.name
    return "%s loads formatter plugins" % path.name if _has_plugins_key(parsed) else None


def _package_json_error(path: Path) -> str | None:
    text = _read_text(path)
    if text is None:
        return "package.json is a link, is too large or could not be read"
    try:
        data = json.loads(text)
    except ValueError:
        return "package.json could not be parsed"
    section = data.get("prettier") if isinstance(data, dict) else None
    if isinstance(section, str):
        return "package.json names a shared prettier config package"
    if isinstance(section, dict) and _has_plugins_key(section):
        return "package.json prettier config loads formatter plugins"
    return None


def _package_yaml_error(path: Path) -> str | None:
    text = _read_text(path)
    if text is None:
        return "package.yaml is a link, is too large or could not be read"
    for match in _PRETTIER_YAML_BLOCK.finditer(text):
        body = match.group(1)
        inline = body.split("\n", 1)[0].split("#", 1)[0].strip()
        if inline and inline[0] not in "{|>":
            return "package.yaml names a shared prettier config package"
        if _text_may_name_plugins(body):
            return "package.yaml prettier config may load formatter plugins"
    return None


def _walk_directories(root: Path, target: str) -> tuple[list[Path], str | None]:
    """The ancestors of the checked path plus every folder beneath it, or an error."""
    found: list[Path] = [root]
    start = root
    walk_below = True
    if target and target != ".":
        start = root / target
        if not start.is_dir():
            start = start.parent
            walk_below = False
    current = start
    while current != root and file_ops._is_inside(current, root):
        found.append(current)
        if current.parent == current:
            break
        current = current.parent
    if not walk_below:
        return found, None
    pending = [start]
    visited = 0
    while pending:
        directory = pending.pop()
        visited += 1
        if visited > _MAX_DIRECTORIES:
            return found, "more than %d folders to check" % _MAX_DIRECTORIES
        if directory not in found:
            found.append(directory)
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name in _PRUNED:
                        continue
                    child = Path(entry.path)
                    if entry.is_symlink() or file_ops.is_reparse_point(child):
                        if child.is_dir():
                            return found, "linked folder %s could hide a config" % entry.name
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(child)
        except OSError:
            return found, "folder %s could not be listed" % directory.name
    return found, None


def _bundled_formatter_error(tool: str, directories: list[Path]) -> str | None:
    package = _NODE_PACKAGES[tool]
    bin_name = package.rsplit("/", 1)[-1]
    for directory in directories:
        modules = directory / "node_modules"
        for candidate in (
            modules / ".bin" / bin_name,
            modules / ".bin" / (bin_name + ".cmd"),
            modules / ".bin" / (bin_name + ".ps1"),
            modules / package,
        ):
            if os.path.lexists(candidate):
                return "the project ships its own %s (node_modules/%s); npx would run that committed code" % (
                    tool, candidate.relative_to(modules).as_posix())
    return None


def executable_config_error(tool: str, root: Path | str, target: str = "") -> str | None:
    """Why a protected check of ``tool`` must not run here, or None."""
    if tool not in _CHECKED_TOOLS:
        return None
    root = Path(root)
    prefix = "refusing to run %s in check mode: " % tool
    directories, walk_error = _walk_directories(root, target)
    if walk_error:
        return prefix + walk_error
    bundled = _bundled_formatter_error(tool, directories)
    if bundled:
        return prefix + bundled
    for directory in directories:
        if tool == "biome":
            for name in _BIOME_CONFIGS:
                if os.path.lexists(directory / name):
                    text = _read_text(directory / name)
                    if text is None or _text_may_name_plugins(text):
                        return prefix + "%s may load formatter plugins or could not be read" % name
            continue
        for name in _EXECUTABLE_PRETTIER:
            if os.path.lexists(directory / name):
                return prefix + "executable project config %s" % name
        for name in _DATA_PRETTIER:
            if os.path.lexists(directory / name):
                reason = _data_config_error(directory / name)
                if reason:
                    return prefix + reason
        for name, check in (("package.json", _package_json_error), ("package.yaml", _package_yaml_error)):
            if os.path.lexists(directory / name):
                reason = check(directory / name)
                if reason:
                    return prefix + reason
    return None
