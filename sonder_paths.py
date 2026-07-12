"""Shared filesystem locations for sonder runtime state.

Install folders can be replaced or duplicated, especially when the Flutter apps
bundle a copy of the system. Runtime state lives in one per-user home directory
unless explicitly overridden.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def default_home() -> Path:
    override = os.environ.get("SONDER_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / "sonder"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "sonder"
    return Path.home() / ".local" / "share" / "sonder"


def ensure_home() -> Path:
    home = default_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def state_path(name: str, env_var: str = "") -> str:
    if env_var:
        override = os.environ.get(env_var, "").strip()
        if override:
            return str(Path(override).expanduser())
    return str(ensure_home() / name)


def memory_db_path() -> str:
    override = os.environ.get("SONDER_DB", "").strip()
    if override:
        return str(Path(override).expanduser())

    target = ensure_home() / "memory.db"
    legacy = Path(__file__).resolve().with_name("memory.db")
    if not target.exists() and legacy.exists() and legacy.resolve() != target.resolve():
        shutil.copy2(legacy, target)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(legacy) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, Path(str(target) + suffix))
    return str(target)
