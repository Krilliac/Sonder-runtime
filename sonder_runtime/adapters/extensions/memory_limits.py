"""Native memory limits for extension child processes.

The adapter deliberately exposes only enforcement, not RSS sampling.  A
 requested limit either gets attached to the child before it is admitted to
 the extension protocol, or startup fails.  Windows uses a Job Object process
 memory limit. Linux uses ``resource.prlimit`` for a hard address-space limit;
 platforms without a native adapter remain explicitly unsupported.
"""
from __future__ import annotations

import os
import subprocess
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


class _PosixLimitToken:
    """The child owns the prlimit after application; there is no parent handle."""

    def close(self) -> None:
        return None


class NativeExtensionMemoryLimiter:
    """Apply an OS-owned hard limit to one extension process."""

    def __init__(self, *, os_module=os, resource_module=None, platform_name=None) -> None:
        self._os = os_module
        self._resource = resource_module
        self._platform_name = platform_name if platform_name is not None else os_module.name

    def apply(self, process: object, limit_bytes: int) -> MemoryLimitToken:
        if self._platform_name == "nt":
            return self._apply_windows(process, limit_bytes)
        if self._platform_name == "posix":
            return self._apply_posix(process, limit_bytes)
        raise ExtensionMemoryLimitUnsupported(
            "native extension memory enforcement is unsupported on this platform"
        )

    def launch_options(
        self,
        memory_limit_bytes: int | None,
        process_limit: int,
    ) -> dict:
        """Return limits that must be installed before child code executes."""
        if isinstance(process_limit, bool) or process_limit < 1:
            raise ExtensionMemoryLimitError("process limit must be positive")
        if self._platform_name == "nt":
            return {"creationflags": getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)}
        if self._platform_name != "posix":
            raise ExtensionMemoryLimitUnsupported(
                "native process containment is unsupported on this platform"
            )
        resource = self._resource
        if resource is None:
            try:
                import resource as resource_module
            except ImportError as exc:
                raise ExtensionMemoryLimitUnsupported(
                    "native POSIX process containment is unavailable"
                ) from exc
            resource = resource_module
        rlimit_as = getattr(resource, "RLIMIT_AS", None)
        rlimit_nproc = getattr(resource, "RLIMIT_NPROC", None)
        if rlimit_nproc is None or (
            memory_limit_bytes is not None and rlimit_as is None
        ):
            raise ExtensionMemoryLimitUnsupported(
                "required POSIX resource limits are unavailable"
            )

        def install_limits() -> None:
            if memory_limit_bytes is not None:
                resource.setrlimit(
                    rlimit_as,
                    (memory_limit_bytes, memory_limit_bytes),
                )
            resource.setrlimit(
                rlimit_nproc,
                (process_limit, process_limit),
            )

        return {"preexec_fn": install_limits}

    def apply_process_limits(
        self,
        process: object,
        memory_limit_bytes: int | None,
        process_limit: int,
    ) -> MemoryLimitToken:
        """Attach post-create OS ownership while the Windows child is suspended."""
        if self._platform_name == "nt":
            return self._apply_windows_job(
                process,
                memory_limit_bytes=memory_limit_bytes,
                process_limit=process_limit,
            )
        if self._platform_name == "posix":
            return _PosixLimitToken()
        raise ExtensionMemoryLimitUnsupported(
            "native process containment is unsupported on this platform"
        )

    def resume(self, process: object) -> None:
        if self._platform_name != "nt":
            return
        import ctypes

        handle = getattr(process, "_handle", None)
        if not handle:
            raise ExtensionMemoryLimitError("process has no native Windows handle")
        resume = ctypes.windll.ntdll.NtResumeProcess
        resume.argtypes = [ctypes.c_void_p]
        resume.restype = ctypes.c_long
        status = int(resume(ctypes.c_void_p(int(handle))))
        if status != 0:
            raise ExtensionMemoryLimitError(
                f"NtResumeProcess failed with status {status}"
            )

    def _apply_posix(self, process: object, limit_bytes: int) -> MemoryLimitToken:
        resource = self._resource
        if resource is None:
            try:
                import resource as resource_module
            except ImportError as exc:
                raise ExtensionMemoryLimitUnsupported(
                    "native extension memory enforcement is unsupported on this platform"
                ) from exc
            resource = resource_module
        prlimit = getattr(resource, "prlimit", None)
        rlimit_as = getattr(resource, "RLIMIT_AS", None)
        pid = getattr(process, "pid", None)
        if prlimit is None or rlimit_as is None or not isinstance(pid, int):
            raise ExtensionMemoryLimitUnsupported(
                "native POSIX address-space enforcement is unavailable"
            )
        try:
            prlimit(pid, rlimit_as, (limit_bytes, limit_bytes))
        except (OSError, ValueError) as exc:
            raise ExtensionMemoryLimitError(
                f"setting native POSIX memory limit failed: {exc}"
            ) from exc
        return _PosixLimitToken()

    @staticmethod
    def _apply_windows(process: object, limit_bytes: int) -> MemoryLimitToken:
        return NativeExtensionMemoryLimiter._apply_windows_job(
            process,
            memory_limit_bytes=limit_bytes,
            process_limit=None,
        )

    @staticmethod
    def _apply_windows_job(
        process: object,
        *,
        memory_limit_bytes: int | None,
        process_limit: int | None,
    ) -> MemoryLimitToken:
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
        info.BasicLimitInformation.LimitFlags = 0x00002000
        if memory_limit_bytes is not None:
            info.BasicLimitInformation.LimitFlags |= 0x00000200
            info.JobMemoryLimit = memory_limit_bytes
        if process_limit is not None:
            info.BasicLimitInformation.LimitFlags |= 0x00000008
            info.BasicLimitInformation.ActiveProcessLimit = process_limit
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
