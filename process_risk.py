"""Bounded, read-only process inventory and memory risk inspection.

This module intentionally exposes only process metadata and aggregate indicator
names/counts.  It never returns command lines, memory bytes, strings, module
paths, or virtual addresses, and it does not request debug privilege.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import Any


OPT_IN_ENV = "SONDER_PROCESS_INSPECTION"
OPT_IN_VALUE = "enabled:bounded-read-only"

_MAX_PROCESSES = 512
_MAX_REGIONS = 512
_MAX_BYTES = 16 * 1024 * 1024
_MAX_SECONDS = 3.0
_READ_CHUNK = 64 * 1024

_INDICATORS: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    (
        "cross_process_memory_write_primitive",
        (b"WriteProcessMemory", b"NtWriteVirtualMemory"),
    ),
    (
        "remote_thread_primitive",
        (b"CreateRemoteThread", b"NtCreateThreadEx", b"RtlCreateUserThread"),
    ),
    (
        "remote_executable_allocation_primitive",
        (b"VirtualAllocEx", b"NtAllocateVirtualMemory"),
    ),
    (
        "reflective_loader_marker",
        (b"ReflectiveLoader", b"ReflectiveDllInjection"),
    ),
    (
        "credential_dump_marker",
        (b"sekurlsa::logonpasswords", b"MiniDumpWriteDump"),
    ),
)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(parsed, maximum))


def _bounded_float(
    value: Any, default: float, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return default
    return max(minimum, min(parsed, maximum))


def _unsupported(operation: str) -> dict[str, Any]:
    return {
        "ok": False,
        "operation": operation,
        "status": "unsupported_platform",
        "platform": os.name,
    }

def list_processes(
    *, max_processes: int = 128, max_seconds: float = 0.5
) -> dict[str, Any]:
    """Return a bounded Windows process inventory without command lines/paths."""
    limit = _bounded_int(max_processes, 128, 1, _MAX_PROCESSES)
    deadline = time.monotonic() + _bounded_float(
        max_seconds, 0.5, 0.05, _MAX_SECONDS
    )
    if os.name != "nt":
        return _unsupported("process_list")

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry),
    )
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry),
    )
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid = ctypes.c_void_p(-1).value
    if snapshot in (None, invalid):
        return {
            "ok": False,
            "operation": "process_list",
            "status": "snapshot_failed",
            "error_code": int(ctypes.get_last_error()),
            "processes": [],
        }

    processes: list[dict[str, Any]] = []
    truncated = False
    timed_out = False
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while more:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if len(processes) >= limit:
                truncated = True
                break
            processes.append(
                {
                    "pid": int(entry.th32ProcessID),
                    "parent_pid": int(entry.th32ParentProcessID),
                    "name": str(entry.szExeFile)[:260],
                    "thread_count": int(entry.cntThreads),
                }
            )
            more = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)

    return {
        "ok": True,
        "operation": "process_list",
        "status": "complete" if not (truncated or timed_out) else "bounded",
        "processes": processes,
        "process_count": len(processes),
        "truncated": truncated,
        "timed_out": timed_out,
        "limits": {"max_processes": limit},
    }


def _risk_summary(counts: dict[str, int]) -> tuple[str, list[str]]:
    present = sorted(name for name, count in counts.items() if count)
    injection = {
        "cross_process_memory_write_primitive",
        "remote_thread_primitive",
        "remote_executable_allocation_primitive",
    }
    matched = len(injection.intersection(present))
    if matched == 3 or "credential_dump_marker" in present:
        return "high", present
    if matched >= 2 or "reflective_loader_marker" in present:
        return "medium", present
    if present:
        return "low", present
    return "none", present


def inspect_process_memory(
    pid: int,
    *,
    max_bytes: int = 4 * 1024 * 1024,
    max_regions: int = 256,
    max_seconds: float = 1.0,
) -> dict[str, Any]:
    """Scan one exact PID for fixed defensive indicators, returning aggregates."""
    if os.environ.get(OPT_IN_ENV) != OPT_IN_VALUE:
        return {
            "ok": False,
            "operation": "process_memory_inspect",
            "status": "opt_in_required",
            "required_environment": OPT_IN_ENV,
        }
    if isinstance(pid, bool):
        parsed_pid = -1
    else:
        try:
            parsed_pid = int(pid)
        except (TypeError, ValueError, OverflowError):
            parsed_pid = -1
    if parsed_pid <= 0 or parsed_pid in (4, os.getpid()):
        return {
            "ok": False,
            "operation": "process_memory_inspect",
            "status": "protected_pid",
            "pid": parsed_pid,
        }
    if os.name != "nt":
        return _unsupported("process_memory_inspect")

    byte_limit = _bounded_int(max_bytes, 4 * 1024 * 1024, 4096, _MAX_BYTES)
    region_limit = _bounded_int(max_regions, 256, 1, _MAX_REGIONS)
    seconds = _bounded_float(max_seconds, 1.0, 0.05, _MAX_SECONDS)
    deadline = time.monotonic() + seconds

    class MemoryBasicInformation(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualQueryEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(MemoryBasicInformation),
        ctypes.c_size_t,
    )
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t
    kernel32.ReadProcessMemory.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    )
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    # PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION.  No write, operation,
    # create-thread, suspend, or all-access rights are requested.
    handle = kernel32.OpenProcess(0x0010 | 0x1000, False, parsed_pid)
    if not handle:
        return {
            "ok": False,
            "operation": "process_memory_inspect",
            "status": "access_denied_or_exited",
            "pid": parsed_pid,
            "error_code": int(ctypes.get_last_error()),
        }

    counts = {name: 0 for name, _ in _INDICATORS}
    bytes_scanned = 0
    regions_examined = 0
    regions_read = 0
    timed_out = False
    address = 0
    max_address = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
    readable = {0x02, 0x04, 0x20, 0x40}
    max_marker = max(len(marker) for _, markers in _INDICATORS for marker in markers)
    try:
        while (
            regions_examined < region_limit
            and bytes_scanned < byte_limit
            and address < max_address
        ):
            if time.monotonic() >= deadline:
                timed_out = True
                break
            mbi = MemoryBasicInformation()
            queried = kernel32.VirtualQueryEx(
                handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)
            )
            if not queried:
                break
            base = int(mbi.BaseAddress or 0)
            size = int(mbi.RegionSize)
            next_address = base + max(size, 1)
            if next_address <= address:
                break
            address = next_address
            regions_examined += 1

            protection = int(mbi.Protect)
            if (
                int(mbi.State) != 0x1000
                or (protection & 0xFF) not in readable
                or protection & (0x100 | 0x01)
            ):
                continue

            region_read = False
            offset = 0
            tail = b""
            while offset < size and bytes_scanned < byte_limit:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                requested = min(_READ_CHUNK, size - offset, byte_limit - bytes_scanned)
                buffer = ctypes.create_string_buffer(requested)
                received = ctypes.c_size_t()
                success = kernel32.ReadProcessMemory(
                    handle,
                    ctypes.c_void_p(base + offset),
                    buffer,
                    requested,
                    ctypes.byref(received),
                )
                got = int(received.value)
                if not success and got == 0:
                    break
                chunk = buffer.raw[:got]
                haystack = tail + chunk
                for name, markers in _INDICATORS:
                    for marker in markers:
                        counts[name] = min(255, counts[name] + haystack.count(marker))
                tail = haystack[-(max_marker - 1) :] if max_marker > 1 else b""
                bytes_scanned += got
                offset += max(got, requested)
                region_read = region_read or got > 0
            if region_read:
                regions_read += 1
            if timed_out:
                break
    finally:
        kernel32.CloseHandle(handle)

    risk, present = _risk_summary(counts)
    bounded = (
        timed_out
        or bytes_scanned >= byte_limit
        or regions_examined >= region_limit
    )
    return {
        "ok": True,
        "operation": "process_memory_inspect",
        "status": "bounded" if bounded else "complete",
        "pid": parsed_pid,
        "risk": risk,
        "indicators": present,
        "indicator_counts": {name: counts[name] for name in present},
        "bytes_scanned": bytes_scanned,
        "regions_examined": regions_examined,
        "regions_read": regions_read,
        "timed_out": timed_out,
        "limits": {
            "max_bytes": byte_limit,
            "max_regions": region_limit,
            "max_seconds": seconds,
        },
    }
