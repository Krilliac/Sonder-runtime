"""Concrete argv process provider wired to the typed tree supervisor."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from ...application.capabilities.jobs import JobCancellationResult, JobRegistryService
from ...application.execution.process_jobs import ProcessJobRequest, ProcessJobStart, ProcessJobWait
from ...application.jobs.durable_registry import DurableJobRegistry, ProcessTreeCleanupContract
from ...application.ports.jobs import JobStatus


class SubprocessJobProvider:
    """Launch one argv process and route its lifecycle through durable jobs.

    The provider creates a new process group on POSIX so the typed supervisor
    can prove descendant cleanup. Windows delegates tree ownership to
    ``taskkill /T`` and does not manufacture a process-group identity.
    """

    def __init__(
        self,
        registry: DurableJobRegistry,
        *,
        process_cleanup: ProcessTreeCleanupContract,
        launcher: Callable[..., Any] | None = None,
        platform_name: str | None = None,
    ) -> None:
        if not isinstance(registry, DurableJobRegistry):
            raise TypeError("registry must be a DurableJobRegistry")
        if not callable(getattr(process_cleanup, "cleanup", None)):
            raise TypeError("process_cleanup must provide cleanup")
        self._registry = registry
        self._jobs = JobRegistryService(registry, process_cleanup=process_cleanup)
        self._launcher = launcher or subprocess.Popen
        self._platform = platform_name or os.name
        self._processes: dict[str, Any] = {}
        self._limits: dict[str, int] = {}

    def start(self, request: ProcessJobRequest) -> ProcessJobStart:
        if not isinstance(request, ProcessJobRequest):
            raise TypeError("request must be a ProcessJobRequest")
        environment = dict(os.environ)
        environment.update(request.environment)
        launch_options: dict[str, Any] = {
            "cwd": None if request.cwd is None else str(Path(request.cwd)),
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        process_group_id: int | None = None
        if self._platform == "posix":
            launch_options["start_new_session"] = True
        elif self._platform == "nt":
            launch_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = self._launcher(list(request.argv), **launch_options)
        process_id = getattr(process, "pid", None)
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
            self._abort_unregistered(process)
            raise RuntimeError("launcher did not return a positive process id")
        if self._platform == "posix":
            process_group_id = process_id
        try:
            record = self._registry.start(
                request.identity,
                process_id=process_id,
                process_group_id=process_group_id,
            )
        except Exception:
            self._abort_unregistered(process)
            raise
        self._processes[request.identity.job_id] = process
        self._limits[request.identity.job_id] = request.max_descendants
        return ProcessJobStart(record, process_id, process_group_id)

    def wait(self, job_id: str, *, timeout: float | None = None) -> ProcessJobWait:
        process = self._process(job_id)
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return ProcessJobWait(self._registry.poll(job_id), None, timed_out=True)
        status = JobStatus.SUCCEEDED if exit_code == 0 else JobStatus.FAILED
        record = self._registry.transition(
            job_id,
            status,
            result={"exit_code": exit_code} if status is JobStatus.SUCCEEDED else None,
            error="process exited with a non-zero status" if status is JobStatus.FAILED else "",
        )
        self._processes.pop(job_id, None)
        self._limits.pop(job_id, None)
        return ProcessJobWait(record, exit_code)

    def cancel(self, job_id: str, reason: str = "cancelled") -> JobCancellationResult:
        limit = self._limits.get(job_id, 64)
        result = self._jobs.cancel_with_cleanup(job_id, reason, max_descendants=limit)
        if result.cleanup_completed:
            process = self._processes.pop(job_id, None)
            self._limits.pop(job_id, None)
            if process is not None:
                try:
                    process.wait(timeout=0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        return result

    def _process(self, job_id: str) -> Any:
        try:
            return self._processes[job_id]
        except KeyError as exc:
            raise KeyError(f"no live process for job {job_id!r}") from exc

    @staticmethod
    def _abort_unregistered(process: Any) -> None:
        try:
            process.kill()
        except (AttributeError, OSError):
            pass
        try:
            process.wait(timeout=1)
        except (AttributeError, OSError, subprocess.TimeoutExpired):
            pass


__all__ = ["SubprocessJobProvider"]
