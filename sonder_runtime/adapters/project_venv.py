"""Validate that a project's virtual-environment interpreter is a real venv launcher.

A repository controls ``.venv/Scripts/python.exe`` (or ``.venv/bin/python``), and
``pip --python <venv>`` re-executes that very binary, so neither a trusted pip
nor the flag avoids running repo-supplied code. The interpreter is therefore
accepted only when it demonstrably is what ``python -m venv`` would have
installed from the base interpreter named by ``pyvenv.cfg``:

- ``home`` in ``pyvenv.cfg`` is an existing directory outside the project root;
- POSIX: the interpreter resolves to a file inside ``home``, or is byte-identical
  to a ``python*`` binary there (``venv --copies``);
- Windows: the interpreter is byte-identical to the base installation's
  ``python.exe`` or to the venv launcher in ``Lib/venv/scripts/nt``.

Anything else (including an unreadable or missing ``home``) is refused. Residual
trust: ``home`` itself, which sits outside the repository the agent controls.
Stateless; any thread.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .filesystem import file_ops

_MAX_CFG_BYTES = 64 * 1024
_MAX_CANDIDATES = 24
_PYTHON_NAME = re.compile(r"python[0-9.]*")


def _digest(path: Path) -> tuple[int, str] | None:
    try:
        hasher = hashlib.sha256()
        size = 0
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                size += len(block)
                hasher.update(block)
        return size, hasher.hexdigest()
    except OSError:
        return None


def _home(cfg: Path) -> Path | None:
    try:
        if cfg.stat().st_size > _MAX_CFG_BYTES:
            return None
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == "home" and value.strip():
            return Path(value.strip())
    return None


def _candidates(home: Path) -> list[Path]:
    if os.name == "nt":
        launchers = home / "Lib" / "venv" / "scripts" / "nt"
        return [home / "python.exe", launchers / "python.exe", launchers / "venvlauncher.exe"]
    try:
        names = sorted(entry for entry in os.listdir(home) if _PYTHON_NAME.fullmatch(entry))
    except OSError:
        return []
    return [home / name for name in names[:_MAX_CANDIDATES]]


def venv_interpreter_is_genuine(root: Path | str, venv: Path, python: Path) -> bool:
    """True when ``python`` (inside ``venv`` under ``root``) is a genuine venv interpreter."""
    home = _home(venv / "pyvenv.cfg")
    if home is None:
        return False
    try:
        real_home = home.resolve(strict=True)
        real_root = Path(root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    if not real_home.is_dir() or real_home == real_root or file_ops._is_inside(real_home, real_root):
        return False
    if not python.is_file():
        return False
    if os.name == "nt":
        if file_ops.is_reparse_point(python):
            return False
    else:
        try:
            real_python = python.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return False
        if file_ops._is_inside(real_python, real_home) and not file_ops._is_inside(real_python, real_root):
            return True
    actual = _digest(python)
    if actual is None:
        return False
    return any(candidate.is_file() and _digest(candidate) == actual for candidate in _candidates(real_home))
