"""Native memory limits for extension child processes.

The adapter deliberately exposes only enforcement, not RSS sampling.  A
requested limit either gets attached to the child before it is admitted to
the extension protocol, or startup fails.  Windows uses a Job Object process
memory limit; other platforms are explicit unsupported until a native
enforcement adapter is added.
"""
from __future__ import annotations

import os
from typing import Protocol


class ExtensionMemoryLimitError(RuntimeError):
    """A requested child memory limit could not be enforced."""


class ExtensionMemoryLimitUnsupported(ExtensionMemoryLimitError):
    """The current platform has no native enforcement adapter."""


class MemoryLimitToken(Protocol):
    def close(self) -> None: ...


class MemoryLimiter(Protocol):
    def apply(self, process: object, limit_bytes: int) -> MemoryLimitToken: ...


class _WindowsJobToken:
    def __init__(self, handle, close_handle) -> None:
        self._handle = handle
        self._close_handle = close_handle

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            self._close_handle(handle)


class NativeExtensionMemoryLimiter:
    """Apply an OS-owned hard limit to one extension process."""

    def apply(self, process: object, limit_bytes: int) -> MemoryLimitToken:
        if os.name != "nt":
            raise ExtensionMemoryLimitUnsupported(
                "native extension memory enforcement is unsupported on this platform"
            )
        return self._apply_windows(process, limit_bytes)

    @staticmethod
    def _apply_windows(process: object, limit_bytes: int) -> MemoryLimitToken:
        import ctypes
        import ctypes.wintypes as wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        handle = getattr(process, "_handle", None)
        if not handle:
            raise ExtensionMemoryLimitError("extension process has no native Windows handle")
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ExtensionMemoryLimitError("CreateJobObjectW failed")
        info = ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = 0x00000100 | 0x00002000
        info.ProcessMemoryLimit = limit_bytes
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            raise ExtensionMemoryLimitError("SetInformationJobObject failed")
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(handle))):
            kernel32.CloseHandle(job)
            raise ExtensionMemoryLimitError("AssignProcessToJobObject failed")
        return _WindowsJobToken(job, kernel32.CloseHandle)


__all__ = [
    "ExtensionMemoryLimitError",
    "ExtensionMemoryLimitUnsupported",
    "MemoryLimiter",
    "MemoryLimitToken",
    "NativeExtensionMemoryLimiter",
]
