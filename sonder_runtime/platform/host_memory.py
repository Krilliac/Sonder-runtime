"""Host memory readings: physical RAM, commit headroom and per-process memory.

Admission needs more than VRAM. A loaded model's runner process keeps weights
mapped, a KV-cache mirror and compute buffers in host memory, and when
available RAM or the commit limit runs out the machine pages and every other
process slows down. This module only *reads*; the floors and decisions live in
``sonder_runtime.domain.gpu_admission``.

It returns platform-local tuples and imports nothing outside this layer (a
platform module may not import the domain); adapters convert the tuples to the
domain's typed readings.

Windows: ``GlobalMemoryStatusEx`` (``ullAvailPhys``; ``ullTotalPageFile`` is the
commit limit and ``ullAvailPageFile`` the commit still available) and
``GetProcessMemoryInfo`` with ``PROCESS_MEMORY_COUNTERS_EX`` (``WorkingSetSize``,
``PrivateUsage``).
Linux: ``/proc/meminfo`` (``MemAvailable``, ``CommitLimit``, ``Committed_AS``)
plus ``vm.overcommit_memory`` (the commit limit is only enforced in mode 2), and
``/proc/<pid>/status`` ``VmRSS`` with ``/proc/<pid>/smaps_rollup`` private pages.
"""
from __future__ import annotations

import os
import sys
from typing import NamedTuple


class HostMemory(NamedTuple):
    available_physical_bytes: int
    total_physical_bytes: int
    commit_limit_bytes: int
    commit_available_bytes: int
    commit_enforced: bool


class ProcessMemory(NamedTuple):
    working_set_bytes: int
    private_bytes: int


_KIB = 1024
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _meminfo_kib(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, rest = line.partition(":")
        fields = rest.split()
        if separator and fields and fields[0].isdigit():
            values[name.strip()] = int(fields[0])
    return values


def parse_meminfo(text: str, overcommit_mode: int | None) -> HostMemory | None:
    """Parse ``/proc/meminfo``; ``None`` when MemAvailable is missing.

    The commit limit is a real constraint only in overcommit mode 2. In modes 0
    and 1 the kernel grants allocations beyond ``CommitLimit``, so the commit
    figures are reported but marked not enforced.
    """
    values = _meminfo_kib(text)
    available = values.get("MemAvailable")
    if available is None:
        return None
    limit = values.get("CommitLimit", 0)
    committed = values.get("Committed_AS", 0)
    return HostMemory(
        available_physical_bytes=available * _KIB,
        total_physical_bytes=values.get("MemTotal", 0) * _KIB,
        commit_limit_bytes=limit * _KIB,
        commit_available_bytes=max(0, limit - committed) * _KIB,
        commit_enforced=overcommit_mode == 2,
    )


def parse_proc_status_rss(text: str) -> int | None:
    values = _meminfo_kib(text)
    return values["VmRSS"] * _KIB if "VmRSS" in values else None


def parse_smaps_private(text: str) -> int | None:
    values = _meminfo_kib(text)
    if "Private_Clean" not in values and "Private_Dirty" not in values:
        return None
    return (values.get("Private_Clean", 0) + values.get("Private_Dirty", 0)) * _KIB


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _read_linux() -> HostMemory | None:
    text = _read_text("/proc/meminfo")
    if text is None:
        return None
    mode_text = (_read_text("/proc/sys/vm/overcommit_memory") or "").strip()
    return parse_meminfo(text, int(mode_text) if mode_text.isdigit() else None)


def _read_windows() -> HostMemory | None:
    # Only reached on win32 (the callers branch on ``sys.platform``). Elsewhere
    # ``ctypes.WinDLL`` does not exist and the callers would map the
    # AttributeError to ``None``; returning ``None`` here is the same result and
    # tells the type checker which platform the body below belongs to.
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(MemoryStatusEx),)
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return HostMemory(
        available_physical_bytes=int(status.ullAvailPhys),
        total_physical_bytes=int(status.ullTotalPhys),
        commit_limit_bytes=int(status.ullTotalPageFile),
        commit_available_bytes=int(status.ullAvailPageFile),
        commit_enforced=True,
    )


def read_host_memory() -> HostMemory | None:
    """Current host memory, or ``None`` when this platform cannot report it."""
    try:
        if sys.platform == "win32":
            return _read_windows()
        if sys.platform.startswith("linux"):
            return _read_linux()
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _process_memory_windows(pid: int) -> ProcessMemory | None:
    # Only reached on win32 (the callers branch on ``sys.platform``). Elsewhere
    # ``ctypes.WinDLL`` does not exist and the callers would map the
    # AttributeError to ``None``; returning ``None`` here is the same result and
    # tells the type checker which platform the body below belongs to.
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.K32GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCountersEx), wintypes.DWORD)
    kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(ProcessMemoryCountersEx)
        if not kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return ProcessMemory(int(counters.WorkingSetSize), int(counters.PrivateUsage))
    finally:
        kernel32.CloseHandle(handle)


def _process_memory_linux(pid: int) -> ProcessMemory | None:
    status = _read_text("/proc/%d/status" % pid)
    resident = parse_proc_status_rss(status) if status is not None else None
    if resident is None:
        return None
    rollup = _read_text("/proc/%d/smaps_rollup" % pid)
    private = parse_smaps_private(rollup) if rollup is not None else None
    # Without smaps_rollup the resident size is the conservative private bound.
    return ProcessMemory(resident, resident if private is None else private)


def process_memory(pid: int) -> ProcessMemory | None:
    """Working set and private bytes of ``pid``; ``None`` when unreadable."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            return _process_memory_windows(pid)
        if sys.platform.startswith("linux"):
            return _process_memory_linux(pid)
    except (OSError, ValueError, AttributeError):
        return None
    return None


def own_process_memory() -> ProcessMemory | None:
    return process_memory(os.getpid())


__all__ = [
    "HostMemory", "ProcessMemory", "own_process_memory", "parse_meminfo",
    "parse_proc_status_rss", "parse_smaps_private", "process_memory",
    "read_host_memory",
]
