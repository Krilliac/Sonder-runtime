"""Trusted child-process helpers for the health-alert adapters.

Executables are addressed by absolute ``%SystemRoot%\\System32`` paths (never
resolved through the current directory or PATH) and children get an allow-listed
environment, so no control-plane secret reaches them.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

_ENV_KEYS = (
    "SystemRoot", "WINDIR", "ComSpec", "PATH", "PATHEXT", "TEMP", "TMP",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramData",
)


def system32_path(*parts: str, environ: Mapping[str, str] | None = None) -> str:
    """Absolute path under System32, or ``""`` when SystemRoot is unknown."""
    source = os.environ if environ is None else environ
    root = source.get("SystemRoot") or source.get("WINDIR")
    if not root:
        return ""
    return os.path.join(root, "System32", *parts)


def minimal_environment(
    extra: Mapping[str, str] | None = None, environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Windows process basics plus ``extra``; nothing else is inherited."""
    source = os.environ if environ is None else environ
    env = {key: source[key] for key in _ENV_KEYS if source.get(key)}
    if extra:
        env.update(extra)
    return env
