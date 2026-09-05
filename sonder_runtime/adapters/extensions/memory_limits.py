"""Native memory limits for extension child processes.

The adapter deliberately exposes only enforcement, not RSS sampling.  A
 requested limit either gets attached to the child before it is admitted to
 the extension protocol, or startup fails.  Windows uses a Job Object process
 memory limit. Linux uses ``resource.prlimit`` for a hard address-space limit;
 platforms without a native adapter remain explicitly unsupported.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
import shutil
import subprocess
import time
from typing import Callable, Mapping, Protocol
from threading import RLock


class ExtensionMemoryLimitError(RuntimeError):
    """A requested child memory limit could not be enforced."""


class ExtensionMemoryLimitUnsupported(ExtensionMemoryLimitError):
    """The current platform has no native enforcement adapter."""


class MemoryLimitToken(Protocol):
    def close(self) -> None: ...


class MemoryLimiter(Protocol):
    def apply(self, process: object, limit_bytes: int) -> MemoryLimitToken: ...


@dataclass(frozen=True, slots=True)
class ProcessContainmentResult:
    """Proof that a job-owned OS containment unit is empty."""

    complete: bool
    forced: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PreparedProcessContainment:
    """Pre-execution argv/options plus the owning containment token."""

    argv: tuple[str, ...]
    launch_options: dict
    token: MemoryLimitToken | None = None
    post_attach_required: bool = False
    metadata: tuple[tuple[str, str], ...] = ()


class _WindowsJobToken:
    def __init__(self, handle, close_handle) -> None:
        self._handle = handle
        self._close_handle = close_handle
        self._lock = RLock()

    def quiesce(self, *, force: bool) -> ProcessContainmentResult:
        """Prove this retained job is empty; never infer membership from PIDs."""
        import ctypes
        from ctypes import wintypes

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        with self._lock:
            if self._handle is None:
                return ProcessContainmentResult(False, detail="job handle is no longer held")
            kernel32 = ctypes.windll.kernel32
            query = kernel32.QueryInformationJobObject
            query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
            query.restype = wintypes.BOOL

            def active():
                result = Accounting()
                if not query(self._handle, 1, ctypes.byref(result), ctypes.sizeof(result), None):
                    raise ExtensionMemoryLimitError("job accounting query failed")
                return result.ActiveProcesses

            if active() == 0:
                return ProcessContainmentResult(True)
            if not force:
                return ProcessContainmentResult(False, detail="owned job still contains processes")
            terminate = kernel32.TerminateJobObject
            terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
            terminate.restype = wintypes.BOOL
            if not terminate(self._handle, 1):
                return ProcessContainmentResult(False, detail="owned job termination failed")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if active() == 0:
                    return ProcessContainmentResult(True, forced=True)
                time.sleep(0.02)
            return ProcessContainmentResult(False, forced=True, detail="owned job cleanup deadline elapsed")

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            if handle:
                result = self._close_handle(handle)
                if result is not None and not result:
                    raise ExtensionMemoryLimitError("owned job handle close failed")
                self._handle = None


class _PosixLimitToken:
    """The child owns the prlimit after application; there is no parent handle."""

    def close(self) -> None:
        return None


class _SystemdScopeToken:
    """Own and prove one transient Linux scope's cgroup lifecycle."""

    _QUIESCENT_STATES = frozenset({"inactive", "failed", "missing"})

    def __init__(
        self,
        *,
        unit_name: str,
        systemctl: str,
        user_scope: bool,
        runner: Callable[..., object],
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
        timeout_seconds: float = 5.0,
    ) -> None:
        self.unit_name = unit_name
        self._systemctl = systemctl
        self._user_scope = user_scope
        self._runner = runner
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._timeout = timeout_seconds
        self._closed = False

    def _command(self, *arguments: str) -> list[str]:
        command = [self._systemctl]
        if self._user_scope:
            command.append("--user")
        command.extend(("--no-ask-password", *arguments))
        return command

    def _run(self, *arguments: str):
        return self._runner(
            self._command(*arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self._timeout,
            check=False,
        )

    def _state(self) -> tuple[str | None, str]:
        try:
            result = self._run(
                "show", "--property=ActiveState", "--value", self.unit_name,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"systemctl show failed: {type(exc).__name__}"
        output = str(getattr(result, "stdout", "") or "").strip().lower()
        detail = str(getattr(result, "stderr", "") or "").strip()
        if getattr(result, "returncode", 1) == 0 and output:
            return output, detail
        missing_text = f"{output}\n{detail}".lower()
        if any(marker in missing_text for marker in (
            "not found", "could not be found", "does not exist", "not loaded",
        )):
            return "missing", detail
        return None, detail or "systemctl did not report scope state"

    def quiesce(self, *, force: bool) -> ProcessContainmentResult:
        state, detail = self._state()
        if state in self._QUIESCENT_STATES:
            return ProcessContainmentResult(True, detail=f"scope is {state}")
        if not force:
            return ProcessContainmentResult(False, detail=detail or f"scope is {state}")
        try:
            killed = self._run("kill", "--signal=SIGKILL", self.unit_name)
        except (OSError, subprocess.SubprocessError) as exc:
            return ProcessContainmentResult(
                False, forced=True,
                detail=f"systemctl scope kill failed: {type(exc).__name__}",
            )
        kill_detail = str(getattr(killed, "stderr", "") or "").strip()
        if getattr(killed, "returncode", 1) != 0:
            missing_text = kill_detail.lower()
            if not any(marker in missing_text for marker in (
                "not found", "could not be found", "does not exist", "not loaded",
            )):
                return ProcessContainmentResult(
                    False, forced=True,
                    detail=kill_detail or "systemctl scope kill was not acknowledged",
                )
        deadline = self._monotonic() + self._timeout
        while True:
            state, detail = self._state()
            if state in self._QUIESCENT_STATES:
                return ProcessContainmentResult(
                    True, forced=True, detail=f"scope cleanup reached {state}",
                )
            if self._monotonic() >= deadline:
                return ProcessContainmentResult(
                    False, forced=True,
                    detail=detail or f"scope remained {state or 'unproven'}",
                )
            self._sleeper(0.05)

    def close(self) -> None:
        if self._closed:
            return
        result = self.quiesce(force=True)
        if not result.complete:
            raise ExtensionMemoryLimitError(result.detail or "scope cleanup is incomplete")
        self._closed = True


class NativeExtensionMemoryLimiter:
    """Apply an OS-owned hard limit to one extension process."""

    def __init__(
        self,
        *,
        os_module=os,
        resource_module=None,
        platform_name=None,
        which: Callable[[str], str | None] = shutil.which,
        command_runner: Callable[..., object] = subprocess.run,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        systemd_user: bool | None = None,
    ) -> None:
        self._os = os_module
        self._resource = resource_module
        self._platform_name = platform_name if platform_name is not None else os_module.name
        self._which = which
        self._command_runner = command_runner
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._systemd_user = systemd_user

    def prepare_process_job(
        self,
        job_id: str,
        argv: tuple[str, ...],
        memory_limit_bytes: int | None,
        process_limit: int,
    ) -> PreparedProcessContainment:
        """Prepare a job-scoped hard process/memory boundary before execution."""
        if not isinstance(job_id, str) or not job_id:
            raise ExtensionMemoryLimitError("job id is required for native containment")
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ExtensionMemoryLimitError("contained argv must be non-empty")
        if isinstance(process_limit, bool) or process_limit < 1:
            raise ExtensionMemoryLimitError("process limit must be positive")
        if self._platform_name == "nt":
            return PreparedProcessContainment(
                argv=tuple(argv),
                launch_options={
                    "creationflags": getattr(subprocess, "CREATE_SUSPENDED", 0x00000004),
                },
                post_attach_required=True,
            )
        if self._platform_name != "posix":
            raise ExtensionMemoryLimitUnsupported(
                "native process containment is unsupported on this platform"
            )
        systemd_run = self._which("systemd-run")
        systemctl = self._which("systemctl")
        if not systemd_run or not systemctl:
            raise ExtensionMemoryLimitUnsupported(
                "strong POSIX process containment requires systemd-run and systemctl"
            )
        user_scope = self._systemd_user
        if user_scope is None:
            environ = getattr(self._os, "environ", os.environ)
            configured = str(environ.get("SONDER_COMPUTE_SYSTEMD_USER", "")).strip().lower()
            if configured:
                if configured not in {"0", "1", "false", "true", "no", "yes"}:
                    raise ExtensionMemoryLimitError(
                        "SONDER_COMPUTE_SYSTEMD_USER must be a boolean"
                    )
                user_scope = configured in {"1", "true", "yes"}
            else:
                getuid = getattr(self._os, "geteuid", None)
                user_scope = not callable(getuid) or int(getuid()) != 0
        unit_stem = "sonder-compute-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:20]
        unit_name = unit_stem + ".scope"
        wrapped = [systemd_run]
        if user_scope:
            wrapped.append("--user")
        wrapped.extend((
            "--no-ask-password",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={unit_stem}",
            f"--property=TasksMax={process_limit}",
        ))
        if memory_limit_bytes is not None:
            wrapped.append(f"--property=MemoryMax={memory_limit_bytes}")
        wrapped.extend(("--", *argv))
        token = _SystemdScopeToken(
            unit_name=unit_name,
            systemctl=systemctl,
            user_scope=bool(user_scope),
            runner=self._command_runner,
            monotonic=self._monotonic,
            sleeper=self._sleeper,
        )
        return PreparedProcessContainment(
            argv=tuple(wrapped),
            launch_options={},
            token=token,
            metadata=(
                ("containment_kind", "systemd_scope"),
                ("containment_unit", unit_name),
                ("containment_user", "1" if user_scope else "0"),
            ),
        )

    def restore_process_job(
        self,
        job_id: str,
        metadata: Mapping[str, object],
    ) -> MemoryLimitToken | None:
        """Rebuild control of a durable systemd scope after provider restart."""
        if metadata.get("containment_kind") != "systemd_scope":
            return None
        unit_name = metadata.get("containment_unit")
        user_value = metadata.get("containment_user")
        expected_unit = (
            "sonder-compute-"
            + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:20]
            + ".scope"
        )
        if (
            not isinstance(unit_name, str)
            or not re.fullmatch(r"sonder-compute-[0-9a-f]{20}\.scope", unit_name)
            or user_value not in {"0", "1"}
        ):
            raise ExtensionMemoryLimitError("persisted systemd scope metadata is invalid")
        if unit_name != expected_unit:
            raise ExtensionMemoryLimitError(
                "persisted systemd scope does not belong to this job"
            )
        systemctl = self._which("systemctl")
        if not systemctl:
            raise ExtensionMemoryLimitUnsupported(
                "restoring strong POSIX containment requires systemctl"
            )
        return _SystemdScopeToken(
            unit_name=unit_name,
            systemctl=systemctl,
            user_scope=user_value == "1",
            runner=self._command_runner,
            monotonic=self._monotonic,
            sleeper=self._sleeper,
        )

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
    "PreparedProcessContainment",
    "ProcessContainmentResult",
]
