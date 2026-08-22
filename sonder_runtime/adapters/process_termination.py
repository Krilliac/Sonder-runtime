"""Bounded process-tree teardown for adapters that own child processes."""
from __future__ import annotations

import os
import signal
import subprocess

from ..application.jobs.durable_registry import (
    ProcessTreeCleanupReceipt,
    ProcessTreeCleanupRequest,
)


class ProcessTreeSupervisor:
    """Execute bounded, truthful process-tree cleanup requests.

    Windows uses ``taskkill /T`` because it is the OS-owned tree operation.
    POSIX requires an explicit process group; killing only a pid would leave
    descendants alive and therefore cannot satisfy the cleanup contract.
    Dependencies are injectable so the adapter can be tested without creating
    or killing real processes.
    """

    def __init__(
        self,
        *,
        os_module=os,
        signal_module=signal,
        subprocess_module=subprocess,
        platform_name: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._os = os_module
        self._signal = signal_module
        self._subprocess = subprocess_module
        self._platform = platform_name or getattr(os_module, "name", "")
        self._timeout = float(timeout_seconds)

    def cleanup(self, request: ProcessTreeCleanupRequest) -> ProcessTreeCleanupReceipt:
        if not isinstance(request, ProcessTreeCleanupRequest):
            raise TypeError("request must be a ProcessTreeCleanupRequest")
        if self._platform == "nt":
            return self._windows_cleanup(request)
        if self._platform == "posix":
            return self._posix_cleanup(request)
        return ProcessTreeCleanupReceipt(
            request.job_id, False, complete=False,
            detail="unsupported platform; full process-tree cleanup is unproven",
        )

    def _windows_cleanup(self, request: ProcessTreeCleanupRequest) -> ProcessTreeCleanupReceipt:
        try:
            result = self._subprocess.run(
                ["taskkill", "/PID", str(request.process_id), "/T", "/F"],
                stdin=self._subprocess.DEVNULL,
                stdout=self._subprocess.DEVNULL,
                stderr=self._subprocess.DEVNULL,
                timeout=self._timeout,
                check=False,
                shell=False,
            )
        except (OSError, self._subprocess.SubprocessError) as exc:
            return ProcessTreeCleanupReceipt(
                request.job_id, True, complete=False,
                detail=f"taskkill failed: {type(exc).__name__}",
            )
        complete = getattr(result, "returncode", 1) == 0
        return ProcessTreeCleanupReceipt(
            request.job_id, True, complete=complete,
            detail="taskkill tree completed" if complete else "taskkill did not confirm tree termination",
        )

    def _posix_cleanup(self, request: ProcessTreeCleanupRequest) -> ProcessTreeCleanupReceipt:
        if request.process_group_id is None:
            return ProcessTreeCleanupReceipt(
                request.job_id, False, complete=False,
                detail="process_group_id is required to prove descendant cleanup",
            )
        try:
            self._os.killpg(request.process_group_id, self._signal.SIGKILL)
        except ProcessLookupError:
            return ProcessTreeCleanupReceipt(
                request.job_id, True, complete=True,
                detail="process group already exited",
            )
        except OSError as exc:
            return ProcessTreeCleanupReceipt(
                request.job_id, True, complete=False,
                detail=f"process-group termination failed: {type(exc).__name__}",
            )
        return ProcessTreeCleanupReceipt(
            request.job_id, True, complete=True,
            detail="process group termination requested",
        )


def terminate_process_tree(
    proc,
    *,
    os_module=os,
    signal_module=signal,
    subprocess_module=subprocess,
) -> None:
    """Best-effort teardown of a process and its ordinary descendants.

    The optional modules keep the compatibility wrapper testable without
    coupling this adapter to the legacy module's imports.
    """
    if proc.poll() is not None:
        return
    pid = getattr(proc, "pid", None)
    if os_module.name == "nt" and pid:
        try:
            subprocess_module.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess_module.DEVNULL,
                stdout=subprocess_module.DEVNULL,
                stderr=subprocess_module.DEVNULL,
                timeout=5,
                check=False,
                shell=False,
            )
            return
        except (OSError, subprocess_module.SubprocessError):
            pass
    elif pid:
        try:
            os_module.killpg(pid, signal_module.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


__all__ = ["ProcessTreeSupervisor", "terminate_process_tree"]
