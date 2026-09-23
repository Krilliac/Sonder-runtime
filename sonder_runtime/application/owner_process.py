"""Fail-closed liveness probe for process-scoped durable reservations."""
from __future__ import annotations

import os
import platform
from collections.abc import Mapping


def process_is_alive(pid: int, host: str) -> bool | None:
    """Return live/dead, or ``None`` when ownership cannot be proved.

    Windows uses ``OpenProcess(SYNCHRONIZE)`` and a zero-time wait.  This is a
    read-only probe; ``os.kill(pid, 0)`` is intentionally not used on Windows
    because it maps to process termination there.
    """
    if type(pid) is not int or pid <= 0 or not isinstance(host, str) or not host.strip():
        return None
    if host.casefold() != platform.node().casefold():
        return None
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        except OSError:
            return None
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        return False if error == 87 or error == 1168 else None
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == 0:
            return False
        if result == 0x102:
            return True
        return None
    finally:
        kernel32.CloseHandle(handle)


def recorded_owner_is_dead(metadata: Mapping[str, str]) -> bool:
    """Prove a recorded owner is dead; malformed/unknown state fails closed."""
    try:
        pid = int(metadata.get("owner_pid", "0"))
    except (TypeError, ValueError):
        return False
    return process_is_alive(pid, metadata.get("owner_host", "")) is False


__all__ = ["process_is_alive", "recorded_owner_is_dead"]
