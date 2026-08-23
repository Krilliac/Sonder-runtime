"""Concrete argv process provider wired to the typed tree supervisor."""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from ...application.capabilities.jobs import JobCancellationResult, JobRegistryService
from ...application.execution.process_jobs import ProcessJobRequest, ProcessJobStart, ProcessJobWait
from ...application.jobs.durable_registry import ProcessTreeCleanupContract
from ...application.jobs.session_lifecycle import JobRegistryLifecycleAdapter
from ...application.execution.world_control import OutputStream
from .durable_output import DurableExecutionOutput
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
        lifecycle: JobRegistryLifecycleAdapter | None = None,
        output: DurableExecutionOutput | None = None,
        inline_output_bytes: int = 16 * 1024,
    ) -> None:
        if not all(callable(getattr(registry, name, None)) for name in (
            "start", "poll", "transition", "append_output", "stream",
        )):
            raise TypeError("registry must provide the durable process-job operations")
        if not callable(getattr(process_cleanup, "cleanup", None)):
            raise TypeError("process_cleanup must provide cleanup")
        if inline_output_bytes < 1:
            raise ValueError("inline_output_bytes must be positive")
        self._registry = registry
        self._jobs = JobRegistryService(registry, process_cleanup=process_cleanup, lifecycle=lifecycle)
        self._launcher = launcher or subprocess.Popen
        self._platform = platform_name or os.name
        self._output = output
        self._inline_output_bytes = inline_output_bytes
        self._processes: dict[str, Any] = {}
        self._limits: dict[str, int] = {}
        self._output_threads: dict[str, tuple[threading.Thread, ...]] = {}
        self._output_failures: dict[str, str] = {}
        self._output_failure_lock = threading.Lock()

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
        self._start_output_readers(request.identity.job_id, process)
        return ProcessJobStart(record, process_id, process_group_id)

    def wait(self, job_id: str, *, timeout: float | None = None) -> ProcessJobWait:
        process = self._process(job_id)
        try:
            readers = self._output_threads.get(job_id, ())
            if readers:
                # Reader threads own the pipes once live output publication is
                # enabled.  Waiting on the process keeps output available to
                # stream consumers before completion and avoids racing a
                # second consumer through ``communicate``.
                exit_code = process.wait(timeout=timeout)
                for reader in readers:
                    reader.join(timeout=1)
            elif callable(getattr(process, "communicate", None)):
                stdout, stderr = process.communicate(timeout=timeout)
                exit_code = getattr(process, "returncode", None)
                if exit_code is None:
                    exit_code = process.wait(timeout=0)
                try:
                    self._record_output(job_id, OutputStream.STDOUT, stdout)
                    self._record_output(job_id, OutputStream.STDERR, stderr)
                except Exception as exc:
                    self._remember_output_failure(job_id, exc)
            else:
                exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return ProcessJobWait(self._registry.poll(job_id), None, timed_out=True)
        output_failure = self._take_output_failure(job_id)
        status = (
            JobStatus.SUCCEEDED
            if exit_code == 0 and output_failure is None
            else JobStatus.FAILED
        )
        error = ""
        if output_failure is not None:
            error = f"process output persistence failed ({output_failure})"
        elif status is JobStatus.FAILED:
            error = "process exited with a non-zero status"
        record = self._registry.transition(
            job_id,
            status,
            result={"exit_code": exit_code} if status is JobStatus.SUCCEEDED else None,
            error=error,
        )
        if self._jobs._lifecycle is not None:
            self._jobs._lifecycle.record(record)
        self._processes.pop(job_id, None)
        self._limits.pop(job_id, None)
        self._output_threads.pop(job_id, None)
        return ProcessJobWait(record, exit_code)

    def cancel(self, job_id: str, reason: str = "cancelled") -> JobCancellationResult:
        limit = self._limits.get(job_id, 64)
        result = self._jobs.cancel_with_cleanup(job_id, reason, max_descendants=limit)
        if self._jobs._lifecycle is not None:
            self._jobs._lifecycle.record_many(result.records)
        if result.cleanup_completed:
            process = self._processes.pop(job_id, None)
            self._limits.pop(job_id, None)
            self._output_threads.pop(job_id, None)
            if process is not None:
                try:
                    process.wait(timeout=0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        return result

    def _start_output_readers(self, job_id: str, process: Any) -> None:
        """Publish stdout/stderr incrementally when the process exposes pipes.

        The provider still supports lightweight process doubles that only
        implement ``wait``/``communicate``.  Real ``Popen`` instances use
        daemon readers so a running job can be streamed through the durable
        registry before ``wait`` finalizes its status.
        """
        readers: list[threading.Thread] = []
        for stream_name, stream in (
            (OutputStream.STDOUT, getattr(process, "stdout", None)),
            (OutputStream.STDERR, getattr(process, "stderr", None)),
        ):
            if not callable(getattr(stream, "readline", None)):
                continue
            reader = threading.Thread(
                target=self._read_output,
                args=(job_id, stream_name, stream),
                name=f"sonder-job-output-{job_id}-{stream_name.value}",
                daemon=True,
            )
            reader.start()
            readers.append(reader)
        if readers:
            self._output_threads[job_id] = tuple(readers)

    def _read_output(self, job_id: str, stream: OutputStream, pipe: Any) -> None:
        try:
            for chunk in iter(pipe.readline, ""):
                if chunk:
                    self._record_output(job_id, stream, chunk)
        except (OSError, ValueError):
            # Process teardown can close a pipe while its reader is waking.
            # The durable job status and already-published watermark remain
            # authoritative; do not turn a normal close into a false failure.
            return
        except Exception as exc:
            # Thread exceptions otherwise disappear after a traceback while
            # wait() reports a successful job.  Keep only the exception type:
            # storage messages can contain paths or operator data.
            self._remember_output_failure(job_id, exc)

    def _record_output(self, job_id: str, stream: OutputStream, data: str | None) -> None:
        if not data:
            return
        payload = str(data)
        encoded_size = len(payload.encode("utf-8"))
        spill = None
        inline = payload
        if encoded_size > self._inline_output_bytes:
            if self._output is not None:
                spill = self._output.spill_text(payload, owner_id=job_id)
            inline = payload[: self._inline_output_bytes]
        self._registry.append_output(job_id, stream, inline, spill=spill)
        if self._jobs._lifecycle is not None:
            page = self._registry.stream(job_id, max_events=1, max_bytes=self._inline_output_bytes)
            if page.events:
                record = self._registry.poll(job_id)
                self._jobs._lifecycle.record_output(record, page.events[-1])

    def _take_output_failure(self, job_id: str) -> str | None:
        with self._output_failure_lock:
            return self._output_failures.pop(job_id, None)

    def _remember_output_failure(self, job_id: str, exc: Exception) -> None:
        with self._output_failure_lock:
            self._output_failures.setdefault(job_id, type(exc).__name__)

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
