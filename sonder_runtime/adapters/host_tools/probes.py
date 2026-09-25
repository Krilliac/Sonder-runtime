"""Injectable host probes used by tool discovery.

Discovery code receives every host interaction through :class:`HostProbes`
so each OS-specific discoverer is testable with fakes on any platform.  The
default probes are thin, bounded wrappers over ``shutil.which``, ``os.stat``,
bounded directory listings and reads, the bounded process runner, and (on
Windows only) a read-only registry reader.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import getpass
import os
import platform as host_platform
import shutil
import stat
import sys
from typing import Callable, Mapping, Protocol

import sonder_runtime.adapters.host_tools.bounded_process as bounded_process
import sonder_runtime.adapters.host_tools.guards as guards
import sonder_runtime.platform.logging as runtime_logging

from .bounded_process import BoundedRun

MAX_LIST_ENTRIES = 512
MAX_SMALL_READ_BYTES = 256 * 1024
MAX_REGISTRY_SUBKEYS = 512


class WindowsRegistry(Protocol):
    def subkeys(self, hive: str, path: str, *, limit: int) -> tuple[str, ...]:
        """Enumerate at most ``limit`` subkey names ("" hive/path errors -> ())."""

    def value(self, hive: str, path: str, name: str) -> str | None:
        """Read one string value, or None."""


@dataclass(frozen=True)
class HostProbes:
    system: str
    env: Mapping[str, str]
    home: str
    user: str
    which: Callable[[str, str], str | None]
    is_file: Callable[[str], bool]
    is_dir: Callable[[str], bool]
    list_dir: Callable[[str, int], tuple[str, ...]]
    stat_identity: Callable[[str], str | None]
    read_small: Callable[[str, int], bytes | None]
    run: Callable[..., BoundedRun]
    registry: WindowsRegistry | None
    project_local: Callable[[str], bool]
    realpath: Callable[[str], str] = field(default=os.path.realpath)
    release: str = ""
    machine: str = ""

    def env_get(self, name: str, default: str = "") -> str:
        """Environment lookup, case-insensitive on Windows."""
        value = self.env.get(name)
        if value is not None:
            return value
        if self.system == "Windows":
            wanted = name.casefold()
            for key, item in self.env.items():
                if key.casefold() == wanted:
                    return item
        return default


# Pinned last over the scrubbed child environment: no colour, no prompts, no
# update checks, no telemetry.  Spec ``probe_env`` (host-owned constants) is
# merged after these.
PINNED_PROBE_ENV: tuple[tuple[str, str], ...] = (
    ("NO_COLOR", "1"),
    ("TERM", "dumb"),
    ("CI", "1"),
    ("CHECKPOINT_DISABLE", "1"),
    ("DOTNET_CLI_TELEMETRY_OPTOUT", "1"),
    ("DOTNET_NOLOGO", "1"),
    ("HOMEBREW_NO_AUTO_UPDATE", "1"),
    ("HOMEBREW_NO_ANALYTICS", "1"),
    ("NO_UPDATE_NOTIFIER", "1"),
    ("npm_config_update_notifier", "false"),
    ("PIP_DISABLE_PIP_VERSION_CHECK", "1"),
    ("GH_NO_UPDATE_NOTIFIER", "1"),
)


def probe_environment(
    probes: "HostProbes", extra: tuple[tuple[str, str], ...] = (),
) -> dict[str, str]:
    """Scrubbed environment for a fixed probe: child policy, pins, then extra."""
    env = runtime_logging.child_environment(dict(probes.env))
    for key, value in PINNED_PROBE_ENV:
        env[key] = value
    for key, value in extra:
        env[key] = value
    return env


def _which(name: str, search_path: str) -> str | None:
    try:
        return shutil.which(name, path=search_path)
    except (OSError, ValueError):
        return None


def _is_file(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except (OSError, ValueError):
        return False


def _is_dir(path: str) -> bool:
    try:
        return os.path.isdir(path)
    except (OSError, ValueError):
        return False


def _list_dir(path: str, limit: int) -> tuple[str, ...]:
    """Sorted names of at most ``limit`` (<= 512) directory entries."""
    limit = max(0, min(int(limit), MAX_LIST_ENTRIES))
    names: list[str] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) >= limit:
                    break
    except (OSError, ValueError):
        return ()
    return tuple(sorted(names))


def _stat_identity(path: str) -> str | None:
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return f"{info.st_size}:{info.st_mtime_ns}"


def _read_small(path: str, limit: int) -> bytes | None:
    """Read a regular file of at most ``limit`` (<= 256 KiB) bytes, no-follow."""
    limit = max(0, min(int(limit), MAX_SMALL_READ_BYTES))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
        return data if len(data) <= limit else None
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def _run(argv, timeout: float, env: Mapping[str, str], *, max_output_chars: int = 2_000) -> BoundedRun:
    return bounded_process.run_bounded(
        tuple(argv), timeout_seconds=timeout, max_output_chars=max_output_chars, env=env,
    )


_HIVES = ("HKLM", "HKCU")


class WinregRegistry:
    """Read-only registry access (KEY_READ | KEY_WOW64_64KEY), Windows only."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("the Windows registry is only available on Windows")
        import winreg  # noqa: PLC0415 - Windows-only optional module

        self._winreg = winreg

    def _open(self, hive: str, path: str):
        winreg = self._winreg
        roots = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}
        if hive not in roots:
            raise OSError("unsupported hive")
        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        return winreg.OpenKey(roots[hive], path, 0, access)

    def subkeys(self, hive: str, path: str, *, limit: int) -> tuple[str, ...]:
        limit = max(0, min(int(limit), MAX_REGISTRY_SUBKEYS))
        names: list[str] = []
        try:
            with self._open(hive, path) as key:
                index = 0
                while len(names) < limit:
                    try:
                        names.append(self._winreg.EnumKey(key, index))
                    except OSError:
                        break
                    index += 1
        except OSError:
            return ()
        return tuple(names)

    def value(self, hive: str, path: str, name: str) -> str | None:
        try:
            with self._open(hive, path) as key:
                data, kind = self._winreg.QueryValueEx(key, name)
        except OSError:
            return None
        if kind not in (self._winreg.REG_SZ, self._winreg.REG_EXPAND_SZ) or not isinstance(data, str):
            return None
        return data[:4096]


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def default_host_probes() -> HostProbes:
    """Probes for the real host.  Building them launches nothing."""
    system = host_platform.system()
    registry = None
    if system == "Windows":
        try:
            registry = WinregRegistry()
        except OSError:
            registry = None
    return HostProbes(
        system=system,
        env=runtime_logging.child_environment(),
        home=os.path.expanduser("~"),
        user=_user(),
        which=_which,
        is_file=_is_file,
        is_dir=_is_dir,
        list_dir=_list_dir,
        stat_identity=_stat_identity,
        read_small=_read_small,
        run=_run,
        registry=registry,
        project_local=guards.project_local,
        realpath=os.path.realpath,
        release=host_platform.release(),
        machine=host_platform.machine(),
    )


__all__ = [
    "HostProbes",
    "PINNED_PROBE_ENV",
    "probe_environment",
    "MAX_LIST_ENTRIES",
    "MAX_REGISTRY_SUBKEYS",
    "MAX_SMALL_READ_BYTES",
    "WindowsRegistry",
    "WinregRegistry",
    "default_host_probes",
]
