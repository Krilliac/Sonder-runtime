"""Shared filesystem locations for sonder runtime state.

Install folders can be replaced or duplicated, especially when the Flutter apps
bundle a copy of the system. Runtime state lives in one per-user home directory
unless explicitly overridden.
"""
from __future__ import annotations

import os
import ntpath
import platform
import re
import shutil
from pathlib import Path


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:$")


def windows_system_drive(env=None) -> str:
    """Return the active Windows system drive, falling back only to ``C:``.

    ``SystemDrive`` is authoritative.  Some stripped-down process environments
    omit it but retain ``SystemRoot``; derive the drive from that before using
    the Windows platform default.  Never guess a data drive such as ``D:``.
    """
    values = os.environ if env is None else env
    drive = str(values.get("SystemDrive", "")).strip()
    if _WINDOWS_DRIVE_RE.fullmatch(drive):
        return drive.upper()
    root_drive = ntpath.splitdrive(str(values.get("SystemRoot", "")))[0]
    if _WINDOWS_DRIVE_RE.fullmatch(root_drive):
        return root_drive.upper()
    return "C:"


def windows_program_files(*, x86=False, env=None) -> str:
    """Resolve Program Files without embedding a particular machine's drive."""
    values = os.environ if env is None else env
    key = "ProgramFiles(x86)" if x86 else "ProgramFiles"
    configured = str(values.get(key, "")).strip()
    if configured:
        return configured
    leaf = "Program Files (x86)" if x86 else "Program Files"
    return ntpath.join(windows_system_drive(values), "\\", leaf)


def default_machine_home(*, env=None, platform_name=None) -> Path:
    """Return the machine-wide Sonder root for the current operating system."""
    values = os.environ if env is None else env
    override = str(values.get("SONDER_MACHINE_HOME", "")).strip()
    if override:
        return Path(override).expanduser()
    if (os.name if platform_name is None else platform_name) == "nt":
        common = (
            str(values.get("PROGRAMDATA", "")).strip()
            or str(values.get("ALLUSERSPROFILE", "")).strip()
        )
        if common:
            return Path(common) / "Sonder"
        return Path(
            ntpath.join(windows_system_drive(values), "\\", "ProgramData", "Sonder")
        )
    return Path("/opt/sonder")


def default_home() -> Path:
    override = os.environ.get("SONDER_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    system = platform.system()
    if system == "Windows" or (not system and os.name == "nt"):
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / "sonder"
        profile = os.environ.get("USERPROFILE", "").strip()
        if profile:
            return Path(profile) / "AppData" / "Local" / "sonder"
        # Service/minimal Windows environments may have no user-profile
        # variables. Honor SystemDrive and use C: only as the final fallback.
        drive = os.environ.get("SystemDrive", "").strip() or "C:"
        suffix = "" if drive.endswith(("\\", "/")) else os.sep
        return Path(drive + suffix) / "Sonder"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "sonder"
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
