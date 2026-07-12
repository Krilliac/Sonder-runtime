"""Cross-platform, non-destructive process-liveness checks."""

from __future__ import annotations

import errno
import operator
import os


def pid_alive(pid: object) -> bool:
    """Return whether *pid* exists without ever signalling it on Windows.

    ``os.kill(pid, 0)`` is a conventional POSIX existence probe, but CPython's
    Windows implementation maps most signals through ``TerminateProcess``.
    Querying a process handle keeps lock inspection and status checks strictly
    read-only on Windows.
    """
    if isinstance(pid, bool):
        return False
    try:
        parsed_pid = int(pid, 10) if isinstance(pid, str) else operator.index(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if parsed_pid <= 0 or parsed_pid > 0xFFFFFFFF:
        return False
    if parsed_pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_alive(parsed_pid)

    try:
        os.kill(parsed_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OverflowError:
        return False
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _windows_pid_alive(pid: int) -> bool:
    try:
        import ctypes

        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        error_access_denied = 5
        wait_timeout = 0x00000102
        wait_failed = 0xFFFFFFFF
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int

        handle = kernel32.OpenProcess(
            process_query_limited_information | synchronize,
            False,
            pid,
        )
        if not handle:
            # A protected process still exists. Treat access denial as live so
            # callers never steal its lease or attempt a replacement launch.
            return ctypes.get_last_error() == error_access_denied
        try:
            wait_result = kernel32.WaitForSingleObject(handle, 0)
            if wait_result == wait_timeout:
                return True
            if wait_result == wait_failed:
                # The query handle exists, so preserve ownership on a transient
                # wait failure rather than risking concurrent work.
                return True
            return False
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, ImportError, OSError, ValueError):
        # Never fall through to os.kill on Windows.
        return False
