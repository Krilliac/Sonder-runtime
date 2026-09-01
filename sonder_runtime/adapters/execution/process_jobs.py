"""Concrete argv process provider wired to the typed tree supervisor."""
from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ...application.capabilities.jobs import JobCancellationResult, JobRegistryService
from ...application.execution.process_jobs import ProcessJobRequest, ProcessJobStart, ProcessJobWait
from ...application.jobs.durable_registry import ProcessTreeCleanupContract
from ...application.jobs.session_lifecycle import JobRegistryLifecycleAdapter
from ...application.execution.world_control import OutputStream
from .durable_output import DurableExecutionOutput
from ...application.ports.jobs import JobStatus
from ..process_liveness import PROCESS_ALIVE, probe_process, process_identity


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
        memory_limiter=None,
        process_identity_resolver=process_identity,
        process_probe=probe_process,
    ) -> None:
        if not all(callable(getattr(registry, name, None)) for name in (
            "start", "attach_process", "poll", "transition", "append_output", "stream",
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
        if memory_limiter is None:
            from ..extensions.memory_limits import NativeExtensionMemoryLimiter
            memory_limiter = NativeExtensionMemoryLimiter(platform_name=self._platform)
        if not callable(getattr(memory_limiter, "apply", None)):
            raise TypeError("memory_limiter must provide apply")
        self._memory_limiter = memory_limiter
        if not callable(process_identity_resolver) or not callable(process_probe):
            raise TypeError("process identity resolver and probe must be callable")
        self._process_identity_resolver = process_identity_resolver
        self._process_probe = process_probe
        self._processes: dict[str, Any] = {}
        self._limits: dict[str, int] = {}
        self._deadline_timers: dict[str, threading.Timer] = {}
        self._memory_tokens: dict[str, Any] = {}
        self._output_threads: dict[str, tuple[threading.Thread, ...]] = {}
        self._output_failures: dict[str, str] = {}
        self._output_failure_lock = threading.Lock()
        self._restore_deadlines()

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
        containment_options = getattr(self._memory_limiter, "launch_options", None)
        apply_process_limits = getattr(self._memory_limiter, "apply_process_limits", None)
        resume_process = getattr(self._memory_limiter, "resume", None)
        native_containment = callable(containment_options) and callable(apply_process_limits)
        if native_containment:
            prepared = containment_options(
                request.memory_limit_bytes,
                request.max_descendants + 1,
            )
            if not isinstance(prepared, dict):
                raise TypeError("native containment launch options must be a mapping")
            if "creationflags" in prepared and "creationflags" in launch_options:
                launch_options["creationflags"] |= int(prepared.pop("creationflags"))
            launch_options.update(prepared)
        deadline_at = None
        if request.deadline_seconds is not None:
            deadline_at = (
                datetime.now(timezone.utc) + timedelta(seconds=request.deadline_seconds)
            ).isoformat()
        persisted_metadata = dict(request.metadata)
        persisted_metadata.update({
            "hard_deadline_at": deadline_at,
            "max_descendants": request.max_descendants,
            "memory_limit_bytes": request.memory_limit_bytes,
            "launch_state": "reserved",
        })
        record = self._registry.start(
            request.identity,
            metadata=persisted_metadata,
        )
        process = None
        memory_token = None
        try:
            process = self._launcher(list(request.argv), **launch_options)
            process_id = getattr(process, "pid", None)
            if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
                raise RuntimeError("launcher did not return a positive process id")
            if self._platform == "posix":
                process_group_id = process_id
            process_instance_identity = self._process_identity_resolver(process_id)
            if request.deadline_seconds is not None and not process_instance_identity:
                raise RuntimeError("deadline jobs require a durable process identity")
            if native_containment:
                memory_token = apply_process_limits(
                    process,
                    request.memory_limit_bytes,
                    request.max_descendants + 1,
                )
            elif request.memory_limit_bytes is not None:
                memory_token = self._memory_limiter.apply(
                    process, request.memory_limit_bytes,
                )
            record = self._registry.attach_process(
                request.identity.job_id,
                process_id=process_id,
                process_group_id=process_group_id,
                metadata={
                    "launch_state": "attached",
                    "process_instance_identity": process_instance_identity,
                },
            )
            if native_containment and callable(resume_process):
                resume_process(process)
        except Exception as exc:
            if memory_token is not None:
                memory_token.close()
            if process is not None:
                self._abort_unregistered(process)
            self._registry.transition(
                request.identity.job_id,
                JobStatus.FAILED,
                error=f"process launch failed ({type(exc).__name__})",
            )
            raise
        self._processes[request.identity.job_id] = process
        self._limits[request.identity.job_id] = request.max_descendants
        if memory_token is not None:
            self._memory_tokens[request.identity.job_id] = memory_token
        self._start_output_readers(request.identity.job_id, process)
        if request.deadline_seconds is not None:
            self._schedule_deadline(request.identity.job_id, request.deadline_seconds)
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
        self._discard_deadline(job_id)
        self._release_memory_limit(job_id)
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
            self._discard_deadline(job_id)
            self._release_memory_limit(job_id)
            if process is not None:
                try:
                    process.wait(timeout=0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        return result

    def poll(self, job_id: str):
        return self._registry.poll(job_id)

    def stream(
        self,
        job_id: str,
        *,
        max_events: int = 32,
        max_bytes: int = 16 * 1024,
    ):
        return self._registry.stream(
            job_id,
            max_events=max_events,
            max_bytes=max_bytes,
        )

    def recover(self, *, kind_prefix: str, limit: int = 1024):
        if not isinstance(kind_prefix, str) or not kind_prefix:
            raise ValueError("kind_prefix is required")
        if isinstance(limit, bool) or not 1 <= limit <= 4096:
            raise ValueError("recovery limit must be within 1..4096")
        iterator = getattr(self._registry, "iter_kind", None)
        records = (
            iterator(kind_prefix, include_terminal=True)
            if callable(iterator)
            else (
                record
                for record in self._registry.list(include_terminal=True, limit=limit)
                if record.identity.kind.startswith(kind_prefix)
            )
        )
        return tuple(
            self._registry.view(record.identity.job_id)
            for record in records
        )

    def _expire_deadline(self, job_id: str) -> None:
        """Cancel a still-live process tree even when its controller vanished."""
        try:
            record = self._registry.poll(job_id)
            if record.is_terminal:
                return
            process = self._processes.get(job_id)
            if process is not None:
                poll = getattr(process, "poll", None)
                if callable(poll) and poll() is not None:
                    self.wait(job_id, timeout=0)
                    return
            else:
                view = self._registry.view(job_id)
                metadata = getattr(view, "metadata", None) or {}
                expected_identity = metadata.get("process_instance_identity")
                process_id = getattr(view, "process_id", None)
                if not isinstance(expected_identity, str) or not expected_identity:
                    self._mark_interrupted(
                        job_id,
                        "deadline owner identity was not durably recorded",
                    )
                    return
                state, observed_identity = self._process_probe(
                    process_id,
                    expected_identity,
                )
                if state != PROCESS_ALIVE or observed_identity != expected_identity:
                    self._mark_interrupted(
                        job_id,
                        "deadline owner process exited or changed identity",
                    )
                    return
            self.cancel(job_id, reason="process deadline exceeded")
        except (KeyError, OSError):
            # A normal completion or concurrent cancellation may win the race.
            return
        finally:
            # The one-shot timer cannot enforce anything after this callback.
            # Cleanup truth remains in the durable record/result; remove only
            # stale in-process scheduler bookkeeping here.
            self._deadline_timers.pop(job_id, None)

    def _schedule_deadline(self, job_id: str, delay_seconds: float) -> None:
        timer = threading.Timer(
            max(0.0, delay_seconds),
            self._expire_deadline,
            args=(job_id,),
        )
        timer.daemon = True
        self._deadline_timers[job_id] = timer
        timer.start()

    def _restore_deadlines(self) -> None:
        """Re-arm persisted hard deadlines after a worker/provider restart."""
        list_jobs = getattr(self._registry, "list", None)
        view_job = getattr(self._registry, "view", None)
        if not callable(list_jobs) or not callable(view_job):
            return
        now = datetime.now(timezone.utc)
        iterator = getattr(self._registry, "iter_kind", None)
        records = (
            iterator("", include_terminal=False)
            if callable(iterator)
            else list_jobs(include_terminal=False, limit=1024)
        )
        for record in records:
            view = view_job(record.identity.job_id)
            metadata = getattr(view, "metadata", None) or {}
            raw_deadline = metadata.get("hard_deadline_at")
            if not isinstance(raw_deadline, str) or not raw_deadline:
                continue
            try:
                deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                limit = int(metadata.get("max_descendants", 64))
                if limit < 1:
                    raise ValueError
            except (TypeError, ValueError):
                self._mark_interrupted(
                    record.identity.job_id,
                    "persisted deadline safety metadata is invalid",
                )
                continue
            process_identity_value = metadata.get("process_instance_identity")
            if not isinstance(process_identity_value, str) or not process_identity_value:
                self._mark_interrupted(
                    record.identity.job_id,
                    "persisted process identity is unavailable after restart",
                )
                continue
            self._limits[record.identity.job_id] = limit
            self._schedule_deadline(
                record.identity.job_id,
                (deadline.astimezone(timezone.utc) - now).total_seconds(),
            )

    def _mark_interrupted(self, job_id: str, detail: str) -> None:
        record = self._registry.transition(
            job_id,
            JobStatus.INTERRUPTED,
            error=detail,
        )
        if self._jobs._lifecycle is not None:
            self._jobs._lifecycle.record(record)

    def _discard_deadline(self, job_id: str) -> None:
        timer = self._deadline_timers.pop(job_id, None)
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()

    def _release_memory_limit(self, job_id: str) -> None:
        token = self._memory_tokens.pop(job_id, None)
        if token is not None:
            token.close()

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
