"""Locate a project's virtual-environment site-packages without running anything in it.

A repository controls everything inside its own ``.venv``: ``python.exe`` can be
a planted binary, ``Lib/site-packages`` can carry ``.pth`` files that execute on
interpreter start (``-I`` does not turn that off) and a committed ``pip`` package
that ``python -m pip`` would import. Nothing under the root is therefore ever
executed. Sonder installs with its OWN pip and ``--target <site-packages>``, so
this module only validates that the venv is a plain directory tree inside the
root and returns the folder packages should land in.

Stateless; any thread. Never follows a link.
"""
from __future__ import annotations

import os
from pathlib import Path

from .filesystem import file_ops

_MAX_CFG_BYTES = 64 * 1024


def _plain_dir(path: Path, root: Path) -> Path | None:
    try:
        if file_ops.is_reparse_point(path) or not path.is_dir():
            return None
        real = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return real if file_ops._is_inside(real, root) else None


def venv_site_packages(root: Path | str, venv: Path) -> Path | None:
    """The ``site-packages`` directory of ``venv`` (under ``root``), or None.

    Requires a regular ``pyvenv.cfg`` and a site-packages directory that is not a
    link and resolves inside ``root``. Executes and imports nothing.
    """
    try:
        real_root = Path(root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    real_venv = _plain_dir(venv, real_root)
    if real_venv is None:
        return None
    cfg = venv / "pyvenv.cfg"
    try:
        if file_ops.is_reparse_point(cfg) or not cfg.is_file() or cfg.stat().st_size > _MAX_CFG_BYTES:
            return None
    except OSError:
        return None
    if os.name == "nt":
        candidates = [venv / "Lib" / "site-packages"]
    else:
        lib = venv / "lib"
        if _plain_dir(lib, real_root) is None:
            return None
        try:
            candidates = [lib / name / "site-packages" for name in sorted(os.listdir(lib)) if name.startswith("python")]
        except OSError:
            return None
    found = [path for path in candidates if _plain_dir(path, real_root) is not None]
    return found[0] if len(found) == 1 else None
