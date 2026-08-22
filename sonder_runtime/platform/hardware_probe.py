"""Pure normalization helpers used by host hardware probes."""

from __future__ import annotations

import platform
import re
import os
from pathlib import Path


def read_text(path: Path) -> str:
    """Read a bounded host-probe text file without leaking filesystem errors."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return ""


def parse_memory_gb(value: object) -> float | None:
    """Parse a human-readable MB/GB memory value into gigabytes."""
    text = str(value or "").strip().lower().replace(",", ".")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(gb|mb)", text)
    if not match:
        return None
    amount = float(match.group(1))
    return amount if match.group(2) == "gb" else amount / 1024.0


def probe_platform() -> str:
    """Return the normalized host platform name without raising."""
    try:
        return platform.system() or "unknown"
    except Exception:
        return "unknown"


def probe_cpu_count() -> int | None:
    """Return the host CPU count without allowing probe failures to escape."""
    try:
        return os.cpu_count()
    except Exception:
        return None


def probe_total_ram_gb() -> float | None:
    """Return total physical RAM in decimal GB, or ``None`` if unknown."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return round(pages * page_size / 1e9, 1)
    except (ValueError, AttributeError, OSError):
        pass

    try:
        import ctypes

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return round(status.ullTotalPhys / 1e9, 1)
    except Exception:
        pass
    return None


__all__ = [
    "parse_memory_gb",
    "read_text",
    "probe_platform",
    "probe_cpu_count",
    "probe_total_ram_gb",
]
