"""Cross-platform, non-destructive process-liveness and identity checks."""

from __future__ import annotations

import errno
import operator
import os
from pathlib import Path
import subprocess
import sys


PROCESS_ALIVE = "alive"
PROCESS_DEAD = "dead"
PROCESS_UNKNOWN = "unknown"

_WINDOWS_RUNNING = "running"
_WINDOWS_TERMINATED = "terminated"
_WINDOWS_ACCESS_DENIED = "access-denied"
_WINDOWS_OPEN_FAILED = "open-failed"
_WINDOWS_WAIT_FAILED = "wait-failed"
_WINDOWS_TIMES_FAILED = "times-failed"
_WINDOWS_API_UNAVAILABLE = "api-unavailable"


def _parse_pid(pid: object) -> int | None:
    if isinstance(pid, bool):
        return None
    try:
        parsed_pid = int(pid, 10) if isinstance(pid, str) else operator.index(pid)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed_pid <= 0 or parsed_pid > 0xFFFFFFFF:
        return None
    return parsed_pid


def _platform_family() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if os.name == "nt":
        return "windows"
    if os.name == "posix":
        return "posix"
    return "other"


def probe_process(
    pid: object, expected_identity: str | None = None,
) -> tuple[str, str | None]:
    """Return ``(state, identity)`` for *pid* without modifying the process.

    When *expected_identity* is supplied, ``PROCESS_ALIVE`` means that exact
    process instance is still running. A different identity at the same live
    PID is reported as ``PROCESS_DEAD`` for the expected owner, while returning
    the replacement process's observed identity.
    """
    parsed_pid = _parse_pid(pid)
    if parsed_pid is None:
        return PROCESS_DEAD, None

    family = _platform_family()
    if family == "windows":
        state, identity = _windows_probe_process(parsed_pid)
    elif family == "linux":
        state, identity = _linux_probe_process(parsed_pid)
    elif family == "posix":
        state, identity = _posix_probe_process(parsed_pid)
    else:
        state, identity = PROCESS_UNKNOWN, None

    if state == PROCESS_ALIVE and expected_identity is not None:
        if identity is None:
            return PROCESS_UNKNOWN, None
        if identity != expected_identity:
            return PROCESS_DEAD, identity
    return state, identity


def pid_alive(pid: object) -> bool:
    """Return conservatively whether *pid* may still be running.

    Probe failures count as live so coordination and launcher callers never steal or
    replace ownership merely because process inspection was unavailable.
    """
    state, _identity = probe_process(pid)
    return state != PROCESS_DEAD


def _windows_native_process_probe(pid: int):
    """Return a raw Windows probe result and creation FILETIME integer."""
    try:
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("low", ctypes.c_uint32),
                ("high", ctypes.c_uint32),
            ]

        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        error_access_denied = 5
        error_invalid_parameter = 87
        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.GetProcessTimes.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        )
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(
            process_query_limited_information | synchronize,
            False,
            pid,
        )
        if not handle:
            error = ctypes.get_last_error()
            if error == error_invalid_parameter:
                return _WINDOWS_TERMINATED, None
            if error == error_access_denied:
                return _WINDOWS_ACCESS_DENIED, None
            return _WINDOWS_OPEN_FAILED, None
        try:
            wait_result = kernel32.WaitForSingleObject(handle, 0)
            if wait_result == wait_object_0:
                return _WINDOWS_TERMINATED, None
            if wait_result != wait_timeout:
                return _WINDOWS_WAIT_FAILED, None

            creation = FileTime()
            exit_time = FileTime()
            kernel = FileTime()
            user = FileTime()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return _WINDOWS_TIMES_FAILED, None
            value = (int(creation.high) << 32) | int(creation.low)
            return _WINDOWS_RUNNING, value
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return _WINDOWS_API_UNAVAILABLE, None


def _windows_probe_process(pid: int):
    outcome, creation_time = _windows_native_process_probe(pid)
    if outcome == _WINDOWS_TERMINATED:
        return PROCESS_DEAD, None
    if outcome != _WINDOWS_RUNNING or creation_time is None:
        return PROCESS_UNKNOWN, None
    return PROCESS_ALIVE, "windows:%d" % creation_time


def _windows_pid_alive(pid: int) -> bool:
    """Compatibility wrapper retained for callers/tests of the old helper."""
    state, _identity = _windows_probe_process(pid)
    return state != PROCESS_DEAD


def _read_linux_stat(pid: int) -> str:
    return Path("/proc/%d/stat" % pid).read_text(
        encoding="ascii", errors="replace"
    )


def _read_linux_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(
        encoding="ascii", errors="replace"
    ).strip()


def _parse_linux_stat(raw: str, expected_pid: int | None = None):
    """Extract state/starttime even when Linux's parenthesized comm has ``)``."""
    if not isinstance(raw, str):
        return None
    start = raw.find("(")
    end = raw.rfind(")")
    if start <= 0 or end <= start:
        return None
    try:
        stat_pid = int(raw[:start].strip())
    except ValueError:
        return None
    if expected_pid is not None and stat_pid != expected_pid:
        return None
    fields = raw[end + 1:].split()
    if len(fields) < 20:
        return None
    state = fields[0]
    starttime = fields[19]
    if len(state) != 1 or not starttime.isdigit():
        return None
    return {"pid": stat_pid, "state": state, "starttime": starttime}


def _linux_probe_process(pid: int):
    try:
        raw = _read_linux_stat(pid)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError) or exc.errno in (
            errno.ENOENT,
            errno.ESRCH,
        ):
            return PROCESS_DEAD, None
        return PROCESS_UNKNOWN, None

    fields = _parse_linux_stat(raw, expected_pid=pid)
    if fields is None:
        return PROCESS_UNKNOWN, None
    if fields["state"] in ("Z", "X", "x"):
        return PROCESS_DEAD, None

    try:
        boot_id = _read_linux_boot_id()
    except OSError:
        return PROCESS_ALIVE, None
    if not boot_id:
        return PROCESS_ALIVE, None
    identity = "linux:%s:%s" % (boot_id, fields["starttime"])
    return PROCESS_ALIVE, identity


def _run_ps(pid: int):
    return subprocess.run(
        ["ps", "-o", "stat=", "-o", "lstart=", "-p", str(pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=2,
        check=False,
    )


def _posix_probe_process(pid: int):
    try:
        result = _run_ps(pid)
    except (OSError, subprocess.SubprocessError, ValueError):
        return PROCESS_UNKNOWN, None

    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    if result.returncode == 1 and not output and not error:
        return PROCESS_DEAD, None
    if result.returncode != 0 or not output:
        return PROCESS_UNKNOWN, None

    first_line = output.splitlines()[0].strip()
    parts = first_line.split(None, 1)
    if not parts:
        return PROCESS_UNKNOWN, None
    if parts[0].startswith("Z"):
        return PROCESS_DEAD, None
    if len(parts) < 2 or not parts[1].strip():
        return PROCESS_ALIVE, None
    return PROCESS_ALIVE, "posix:%s" % parts[1].strip()
