"""Refuse project formatter configuration that executes code (protected check mode).

``prettier --check`` loads ``prettier.config.js`` (or ``.prettierrc.mjs``, or a
``plugins`` entry in a data config, or a shared-config package named in
``package.json``) in the process that runs it, so a "read-only" check of a
hostile repository is code execution. ``format_code(check_only=True)`` calls
:func:`executable_config_error` before it starts a formatter and refuses when
any directory from the checked path up to the root carries such a config.

Stateless; safe from any thread. Reads only small regular files and never
follows a link (a linked config is itself refused).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .filesystem import file_ops

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
_PLUGINS_KEY = re.compile(r"""["']?plugins["']?\s*[:=]""", re.IGNORECASE)
_MAX_CONFIG_BYTES = 256 * 1024
_CHECKED_TOOLS = frozenset({"prettier"})


def _directories(root: Path, target: str) -> list[Path]:
    """The root and every directory from the checked path up to it."""
    root = Path(root)
    found = [root]
    if not target or target == ".":
        return found
    current = root / target
    if not current.is_dir():
        current = current.parent
    while current != root and file_ops._is_inside(current, root):
        found.append(current)
        if current.parent == current:
            break
        current = current.parent
    return found


def _data_config_error(path: Path) -> str | None:
    try:
        if file_ops.is_reparse_point(path) or path.stat().st_size > _MAX_CONFIG_BYTES:
            return "%s is a link or is too large to inspect" % path.name
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "%s could not be read" % path.name
    if _PLUGINS_KEY.search(text):
        return "%s loads formatter plugins" % path.name
    return None


def _package_json_error(path: Path) -> str | None:
    try:
        if file_ops.is_reparse_point(path) or path.stat().st_size > _MAX_CONFIG_BYTES:
            return "package.json is a link or is too large to inspect"
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return "package.json could not be read"
    section = data.get("prettier") if isinstance(data, dict) else None
    if isinstance(section, str):
        return "package.json names a shared prettier config package"
    if isinstance(section, dict) and _PLUGINS_KEY.search(json.dumps(section)):
        return "package.json prettier config loads formatter plugins"
    return None


def executable_config_error(tool: str, root: Path | str, target: str = "") -> str | None:
    """Why a protected check of ``tool`` must not run here, or None."""
    if tool not in _CHECKED_TOOLS:
        return None
    for directory in _directories(Path(root), target):
        for name in _EXECUTABLE_PRETTIER:
            if os.path.lexists(directory / name):
                return "refusing to run %s in check mode: executable project config %s" % (tool, name)
        for name in _DATA_PRETTIER:
            candidate = directory / name
            if os.path.lexists(candidate):
                reason = _data_config_error(candidate)
                if reason:
                    return "refusing to run %s in check mode: %s" % (tool, reason)
        package = directory / "package.json"
        if os.path.lexists(package):
            reason = _package_json_error(package)
            if reason:
                return "refusing to run %s in check mode: %s" % (tool, reason)
    return None
