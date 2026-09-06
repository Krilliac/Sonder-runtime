"""Concrete argv process provider wired to the typed tree supervisor."""
from __future__ import annotations

from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

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
from ...platform import logging as runtime_logging
from ..process_liveness import PROCESS_ALIVE, probe_process, process_identity
from ..extensions.memory_limits import (
    PreparedProcessContainment,
    ProcessContainmentResult,
)


class _ProcessSlotLease:
    """One acquired semaphore slot, returned at most once across cleanup races."""

    def __init__(self, semaphore):
        self._semaphore = semaphore
        self._lock = threading.Lock()
        self._released = False

    def release(self):
        with self._lock:
            if not self._released:
                self._released = True
                if self._semaphore is not None:
                    self._semaphore.release()


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
        timer_factory=threading.Timer,
        cleanup_retry_seconds: float = 1.0,
        max_concurrent_processes: int | None = None,
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
        if not callable(timer_factory):
            raise TypeError("timer_factory must be callable")
        if cleanup_retry_seconds <= 0 or cleanup_retry_seconds > 60:
            raise ValueError("cleanup_retry_seconds must be within (0, 60]")
        self._timer_factory = timer_factory
        self._cleanup_retry_seconds = float(cleanup_retry_seconds)
        self._processes: dict[str, Any] = {}
        self._limits: dict[str, int] = {}
        self._deadline_timers: dict[str, threading.Timer] = {}
        self._launch_locks: dict[str, threading.RLock] = {}
        self._memory_tokens: dict[str, Any] = {}
        self._unresolved_scopes: dict[str, dict[str, Any]] = {}
        self._output_threads: dict[str, tuple[threading.Thread, ...]] = {}
        self._output_failures: dict[str, str] = {}
        self._output_failure_lock = threading.Lock()
        self._timer_lock = threading.RLock()
        self._max_concurrent_processes = max_concurrent_processes
        self._process_slots: threading.BoundedSemaphore | None = (
            threading.BoundedSemaphore(max_concurrent_processes)
            if max_concurrent_processes is not None and max_concurrent_processes >= 1
            else None
        )
        self._process_slot_owners: dict[str, _ProcessSlotLease] = {}
        self._failed_launches: set[str] = set()
        self._cleanup_observations: dict[str, int] = {}
        self._restore_deadlines()

    def start(self, request: ProcessJobRequest) -> ProcessJobStart:
        if not isinstance(request, ProcessJobRequest):
            raise TypeError("request must be a ProcessJobRequest")
        if self._process_slots is not None and not self._process_slots.acquire(blocking=False):
            raise RuntimeError(
                f"tool process capacity exhausted ({self._max_concurrent_processes} concurrent)"
            )
        lease = _ProcessSlotLease(self._process_slots)
        try:
            return self._start_reserved(request, lease)
        except BaseException:
            # A launched process with unresolved teardown retains its lease.
            # Only pre-launch or proven-clean failures return local capacity.
            with self._timer_lock:
                retained = self._process_slot_owners.get(request.identity.job_id) is lease
            if not retained:
                lease.release()
            raise

    def _start_reserved(self, request, lease):
        environment = runtime_logging.child_environment() if request.inherit_environment else {}
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
        launch_argv = tuple(request.argv)
        prepared_scope: PreparedProcessContainment | None = None
        containment_options = getattr(self._memory_limiter, "launch_options", None)
        apply_process_limits = getattr(self._memory_limiter, "apply_process_limits", None)
        resume_process = getattr(self._memory_limiter, "resume", None)
        native_containment = False
        if request.require_job_scope:
            prepare_scope = getattr(self._memory_limiter, "prepare_process_job", None)
            if not callable(prepare_scope):
                raise RuntimeError("strong process containment is not configured")
            prepared_scope = prepare_scope(
                request.identity.job_id,
                launch_argv,
                request.memory_limit_bytes,
                request.max_descendants + 1,
            )
            if not isinstance(prepared_scope, PreparedProcessContainment):
                raise TypeError("native job containment returned an invalid preparation")
            isolate_environment = getattr(self._memory_limiter, "isolated_process_environment", None)
            if not request.inherit_environment and callable(isolate_environment):
                prepared_scope = isolate_environment(prepared_scope, launch_argv, environment)
                if not isinstance(prepared_scope, PreparedProcessContainment):
                    raise TypeError("native isolated containment returned an invalid preparation")
            launch_argv = prepared_scope.argv
            prepared_options = dict(prepared_scope.launch_options)
            if "creationflags" in prepared_options and "creationflags" in launch_options:
                launch_options["creationflags"] |= int(prepared_options.pop("creationflags"))
            launch_options.update(prepared_options)
        elif (
            request.memory_limit_bytes is not None
            and callable(containment_options)
            and callable(apply_process_limits)
        ):
            native_containment = True
            prepared_options = containment_options(
                request.memory_limit_bytes,
                request.max_descendants + 1,
            )
            if not isinstance(prepared_options, dict):
                raise TypeError("native containment launch options must be a mapping")
            prepared_options = dict(prepared_options)
            if "creationflags" in prepared_options and "creationflags" in launch_options:
                launch_options["creationflags"] |= int(prepared_options.pop("creationflags"))
            launch_options.update(prepared_options)
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
            "require_job_scope": "1" if request.require_job_scope else "0",
        })
        if prepared_scope is not None:
            persisted_metadata.update(dict(prepared_scope.metadata))
        record = self._registry.start(
            request.identity,
            metadata=persisted_metadata,
        )
        process = None
        memory_token = None if prepared_scope is None else prepared_scope.token
        launch_lock = threading.RLock()
        with self._timer_lock:
            self._launch_locks[request.identity.job_id] = launch_lock
        if memory_token is not None:
            self._limits[request.identity.job_id] = request.max_descendants
            self._memory_tokens[request.identity.job_id] = memory_token
        launch_lock.acquire()
        capacity_dispatched = False
        try:
            if deadline_at is not None:
                self._schedule_deadline_at(request.identity.job_id, deadline_at)
            current = self._registry.poll(request.identity.job_id)
            if current.is_terminal or current.status is JobStatus.CANCELLATION_REQUESTED:
                raise RuntimeError("process deadline expired before launch")
            if request.capacity_token is not None:
                self._registry.dispatch_capacity(request.identity.job_id, request.capacity_token)
                capacity_dispatched = True
            process = self._launcher(list(launch_argv), **launch_options)
            process_id = getattr(process, "pid", None)
            if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
                raise RuntimeError("launcher did not return a positive process id")
            if self._platform == "posix":
                process_group_id = process_id
            process_instance_identity = self._process_identity_resolver(process_id)
            exited_before_fingerprint = False
            if request.deadline_seconds is not None and not process_instance_identity:
                # A live process without a durable identity is refused: a
                # deadline that fired later could kill whatever recycled its
                # pid. A child that has already exited (a zombie by the time
                # the probe reads it, or reaped by a wrapper) is different:
                # there is nothing left for the deadline to enforce, and its
                # exit code and output are still the job's result. Refusing
                # it reported a finished job as a failed launch.
                poll = getattr(process, "poll", None)
                if not callable(poll) or poll() is None:
                    raise RuntimeError("deadline jobs require a durable process identity")
                exited_before_fingerprint = True
                process_instance_identity = ""
            if prepared_scope is not None and prepared_scope.post_attach_required:
                if not callable(apply_process_limits):
                    raise RuntimeError("post-create process containment is not configured")
                memory_token = apply_process_limits(
                    process,
                    request.memory_limit_bytes,
                    request.max_descendants + 1,
                )
            elif native_containment:
                memory_token = apply_process_limits(
                    process,
                    request.memory_limit_bytes,
                    request.max_descendants + 1,
                )
            elif memory_token is None and request.memory_limit_bytes is not None:
                # A prepared scope already owns both process and memory limits.
                # Replacing its token loses descendant containment and cleanup proof.
                memory_token = self._memory_limiter.apply(
                    process, request.memory_limit_bytes,
                )
            if memory_token is not None:
                self._limits[request.identity.job_id] = request.max_descendants
                self._memory_tokens[request.identity.job_id] = memory_token
            if self._registry.poll(request.identity.job_id).status is JobStatus.CANCELLATION_REQUESTED:
                raise RuntimeError("process deadline expired during launch")
            record = self._registry.attach_process(
                request.identity.job_id,
                process_id=process_id,
                process_group_id=process_group_id,
                metadata={
                    "launch_state": "attached",
                    "containment_identity": getattr(memory_token, "identity", ""),
                    "process_instance_identity": process_instance_identity,
                    "process_exited_before_fingerprint": (
                        "1" if exited_before_fingerprint else "0"
                    ),
                },
            )
            if (
                (native_containment or (
                    prepared_scope is not None and prepared_scope.post_attach_required
                ))
                and callable(resume_process)
            ):
                resume_process(process)
            with self._timer_lock:
                # A consumer may complete a process as soon as it is visible.
                # Its capacity lease must already be available for release.
                self._process_slot_owners[request.identity.job_id] = lease
                self._processes[request.identity.job_id] = process
            self._limits[request.identity.job_id] = request.max_descendants
            if memory_token is not None:
                self._memory_tokens[request.identity.job_id] = memory_token
            self._start_output_readers(request.identity.job_id, process)
        except BaseException as exc:
            if process is not None:
                # Publish unresolved ownership before cleanup can itself fail.
                with self._timer_lock:
                    self._failed_launches.add(request.identity.job_id)
                    self._process_slot_owners[request.identity.job_id] = lease
                    self._processes[request.identity.job_id] = process
                try:
                    process_exited = self._abort_unregistered(process)
                except Exception:
                    process_exited = False
                try:
                    containment = self._quiesce_containment(
                        request.identity.job_id, force=True,
                    )
                except Exception:
                    containment = ProcessContainmentResult(
                        False, detail="process launch containment cleanup failed",
                    )
            else:
                process_exited = True
                containment = None
            cleanup_complete = process_exited and (containment is None or containment.complete)
            if memory_token is not None and cleanup_complete:
                try:
                    memory_token.close()
                except Exception:
                    if process is not None:
                        cleanup_complete = False
                else:
                    self._memory_tokens.pop(request.identity.job_id, None)
            current = self._registry.poll(request.identity.job_id)
            # A throwing launcher has not returned a process handle; retain
            # the durable reservation unless containment proves the scope empty.
            if capacity_dispatched and containment is not None and cleanup_complete:
                self._release_capacity(request.identity.job_id)
            if cleanup_complete:
                self._processes.pop(request.identity.job_id, None)
                self._failed_launches.discard(request.identity.job_id)
                self._release_process_slot(request.identity.job_id)
                self._limits.pop(request.identity.job_id, None)
                self._discard_deadline(request.identity.job_id)
                if not current.is_terminal:
                    try:
                        if current.status is JobStatus.CANCELLATION_REQUESTED:
                            self._jobs.cancel(
                                request.identity.job_id,
                                "process deadline expired during launch",
                                max_descendants=request.max_descendants,
                            )
                        else:
                            self._registry.transition(
                                request.identity.job_id,
                                JobStatus.FAILED,
                                error=f"process launch failed ({type(exc).__name__})",
                            )
                    except Exception:
                        pass
            else:
                self._jobs.request_cancellation(
                    request.identity.job_id,
                    (
                        containment.detail
                        if containment is not None and containment.detail
                        else "process launch cleanup is incomplete"
                    ),
                    max_descendants=request.max_descendants,
                )
                self._schedule_deadline(
                    request.identity.job_id, self._cleanup_retry_seconds,
                )
            raise
        finally:
            launch_lock.release()
            with self._timer_lock:
                if self._launch_locks.get(request.identity.job_id) is launch_lock:
                    self._launch_locks.pop(request.identity.job_id, None)
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
        containment = self._quiesce_containment(job_id, force=True)
        if containment is not None and not containment.complete:
            records = self._jobs.request_cancellation(
                job_id,
                containment.detail or "process containment cleanup is incomplete",
                max_descendants=self._limits.get(job_id, 64),
            )
            self._schedule_deadline(job_id, self._cleanup_retry_seconds)
            return ProcessJobWait(records[-1], exit_code)
        current = self._registry.poll(job_id)
        if containment is not None and (
            containment.forced or current.status is JobStatus.CANCELLATION_REQUESTED
        ):
            reason = (
                containment.detail
                or "job scope required forced descendant cleanup after process exit"
            )
            if current.status is not JobStatus.CANCELLATION_REQUESTED:
                self._jobs.request_cancellation(
                    job_id, reason, max_descendants=self._limits.get(job_id, 64),
                )
            records = self._jobs.cancel(
                job_id, reason, max_descendants=self._limits.get(job_id, 64),
            )
            self._cleanup_observations[job_id] = exit_code
            try:
                self._forget_local_job(job_id)
                self._publish_cleanup(job_id, exit_code)
            except Exception:
                self._schedule_deadline(job_id, self._cleanup_retry_seconds)
                raise
            return ProcessJobWait(records[-1], exit_code)
        if containment is not None or job_id in self._cleanup_observations:
            self._cleanup_observations[job_id] = exit_code
            try:
                self._release_memory_limit(job_id)
                self._release_capacity(job_id)
            except Exception:
                self._schedule_deadline(job_id, self._cleanup_retry_seconds)
                raise
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
        self._failed_launches.discard(job_id)
        self._limits.pop(job_id, None)
        self._output_threads.pop(job_id, None)
        self._discard_deadline(job_id)
        self._release_process_slot(job_id)
        if containment is None:
            self._release_memory_limit(job_id)
        if job_id in self._cleanup_observations:
            try:
                self._publish_cleanup(job_id, self._cleanup_observations[job_id])
            except Exception:
                self._schedule_deadline(job_id, self._cleanup_retry_seconds)
                raise
        return ProcessJobWait(record, exit_code)

    def cancel(self, job_id: str, reason: str = "cancelled") -> JobCancellationResult:
        with self._timer_lock:
            launch_lock = self._launch_locks.get(job_id)
        if launch_lock is None:
            return self._cancel_owned(job_id, reason)
        with launch_lock:
            return self._cancel_owned(job_id, reason)

    def _cancel_owned(self, job_id: str, reason: str) -> JobCancellationResult:
        limit = self._limits.get(job_id, 64)
        process_exited = True
        if job_id in self._failed_launches:
            process = self._processes.get(job_id)
            process_exited = process is not None and self._abort_unregistered(process)
        unresolved_scope = self._unresolved_scopes.get(job_id)
        if unresolved_scope is not None and not self._restore_scope_owner(
            job_id, unresolved_scope,
        ):
            records = self._jobs.request_cancellation(
                job_id,
                reason,
                max_descendants=limit,
            )
            result = JobCancellationResult(
                records,
                cleanup_completed=False,
                detail="persisted job scope ownership is unresolved",
            )
            if self._jobs._lifecycle is not None:
                self._jobs._lifecycle.record_many(result.records)
            self._schedule_deadline(job_id, self._cleanup_retry_seconds)
            return result
        containment = self._quiesce_containment(job_id, force=True)
        if not process_exited or (containment is not None and not containment.complete):
            records = self._jobs.request_cancellation(
                job_id,
                reason,
                max_descendants=limit,
            )
            result = JobCancellationResult(
                records,
                cleanup_completed=False,
                detail=(containment.detail if containment is not None else "")
                or "process exit or job scope cleanup is incomplete",
            )
        elif containment is not None:
            current = self._registry.poll(job_id)
            if current.status is not JobStatus.CANCELLATION_REQUESTED:
                self._jobs.request_cancellation(
                    job_id,
                    reason,
                    max_descendants=limit,
                )
            records = self._jobs.cancel(job_id, reason, max_descendants=limit)
            result = JobCancellationResult(
                records,
                cleanup_completed=True,
                detail=containment.detail,
            )
        else:
            result = self._jobs.cancel_with_cleanup(job_id, reason, max_descendants=limit)
        if self._jobs._lifecycle is not None:
            self._jobs._lifecycle.record_many(result.records)
        if result.cleanup_completed:
            process = self._processes.get(job_id)
            observed_exit = None
            if process is not None:
                try:
                    observed_exit = process.wait(timeout=0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            if process is not None and observed_exit is None:
                self._schedule_deadline(job_id, self._cleanup_retry_seconds)
                return JobCancellationResult(
                    result.records,
                    cleanup_completed=False,
                    detail="root process exit remains unobserved",
                )
            if (
                containment is not None
                and containment.complete
                and observed_exit is not None
            ):
                self._cleanup_observations[job_id] = observed_exit
            try:
                self._forget_local_job(job_id)
                if job_id in self._cleanup_observations:
                    self._publish_cleanup(job_id, self._cleanup_observations[job_id])
            except Exception:
                self._schedule_deadline(job_id, self._cleanup_retry_seconds)
                raise
        else:
            self._schedule_deadline(job_id, self._cleanup_retry_seconds)
        return result

    def _publish_cleanup(self, job_id, exit_code):
        """Publish only from the provider's observed exit/containment release path."""
        import hashlib
        import json

        view = self._registry.view(job_id)
        metadata = view.metadata or {}
        if metadata.get("require_job_scope") != "1":
            self._cleanup_observations.pop(job_id, None)
            return
        if any(
            job_id in collection
            for collection in (
                self._memory_tokens,
                self._unresolved_scopes,
                self._process_slot_owners,
            )
        ):
            raise RuntimeError("process resources remain owned")
        proof = dict(
            job_id=job_id,
            job_revision=view.record.revision,
            parent_session_id=view.record.identity.parent_session_id,
            principal_id=metadata.get("principal_id", ""),
            process_id=view.process_id,
            process_identity=metadata.get("process_instance_identity"),
            scope_identity={
                k: v
                for k, v in metadata.items()
                if k.startswith("containment_") or k.startswith("job_scope")
            },
            process_exited=True,
            containment_empty=True,
            resources_released=True,
            status=view.record.status.value,
            exit_code=exit_code,
        )
        proof["digest"] = hashlib.sha256(
            json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self._registry._record_process_cleanup(job_id, proof)
        self._cleanup_observations.pop(job_id, None)

    def cleanup_proof(self, job_id):
        read = getattr(self._registry, "process_cleanup_proof", None)
        proof = None if read is None else read(job_id)
        if proof is None:
            return None
        record = self._registry.poll(job_id)
        if (
            not record.is_terminal
            or record.revision != proof.get("job_revision")
            or record.status.value != proof.get("status")
            or record.identity.parent_session_id != proof.get("parent_session_id")
        ):
            return None
        return proof

    def poll(self, job_id: str):
        return self._registry.poll(job_id)

    def snapshot_output_readers(self, job_id: str):
        """Retain exact owned handles for a containing host's cleanup proof."""
        with self._timer_lock:
            return tuple(self._output_threads.get(job_id, ()))

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
        self._discard_deadline(job_id)
        with self._timer_lock:
            launch_lock = self._launch_locks.get(job_id)
        if launch_lock is None:
            self._expire_deadline_owned(job_id)
            return
        with launch_lock:
            self._expire_deadline_owned(job_id)

    def _expire_deadline_owned(self, job_id: str) -> None:
        retry_cleanup = False
        try:
            record = self._registry.poll(job_id)
            if record.is_terminal:
                if job_id in self._cleanup_observations:
                    exit_code = self._cleanup_observations[job_id]
                    self._forget_local_job(job_id)
                    self._publish_cleanup(job_id, exit_code)
                else:
                    # A completed tree kill can race the provider's zero-time
                    # wait: the signal is accepted, but the root has not been
                    # reaped yet.  On retry the platform identity probe may
                    # correctly report that the child is gone, so do not ask
                    # the supervisor to validate an already-completed cleanup
                    # a second time.  A local process handle that reports an
                    # exit is sufficient to finish the provider-owned reap;
                    # a live or unobservable handle still follows the normal
                    # fail-closed retry path below.
                    process = self._processes.get(job_id)
                    poll = getattr(process, "poll", None) if process is not None else None
                    try:
                        process_exited = callable(poll) and poll() is not None
                    except Exception:
                        process_exited = False
                    if process_exited:
                        self.wait(job_id, timeout=0)
                    elif self._owns_cleanup_resources(job_id):
                        self.cancel(job_id, reason="terminal job resource cleanup retry")
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
                if job_id in self._unresolved_scopes:
                    self.cancel(job_id, reason="process deadline exceeded")
                    return
                if job_id in self._memory_tokens:
                    self.cancel(job_id, reason="process deadline exceeded")
                    return
                if metadata.get("launch_state") == "reserved":
                    self._jobs.request_cancellation(
                        job_id,
                        "process deadline exceeded during launch",
                        max_descendants=self._limits.get(job_id, 64),
                    )
                    retry_cleanup = True
                    return
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
        except KeyError:
            # A normal completion or concurrent cancellation may win the race.
            return
        except Exception:
            retry_cleanup = True
        finally:
            if retry_cleanup:
                try:
                    if (
                        not self._registry.poll(job_id).is_terminal
                        or self._owns_cleanup_resources(job_id)
                        or job_id in self._cleanup_observations
                    ):
                        self._schedule_deadline(job_id, self._cleanup_retry_seconds)
                except KeyError:
                    pass

    def _owns_cleanup_resources(self, job_id):
        return any(
            job_id in collection
            for collection in (
                self._processes,
                self._memory_tokens,
                self._unresolved_scopes,
                self._process_slot_owners,
            )
        )

    def _schedule_deadline(self, job_id: str, delay_seconds: float) -> None:
        timer = self._timer_factory(
            max(0.0, delay_seconds),
            self._expire_deadline,
            args=(job_id,),
        )
        timer.daemon = True
        with self._timer_lock:
            prior = self._deadline_timers.get(job_id)
            if prior is not None and prior is not threading.current_thread():
                prior.cancel()
            self._deadline_timers[job_id] = timer
        timer.start()

    def _schedule_deadline_at(self, job_id: str, raw_deadline: str) -> None:
        deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        delay = (deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        self._schedule_deadline(job_id, delay)

    def _restore_deadlines(self) -> None:
        """Re-arm persisted hard deadlines after a worker/provider restart."""
        list_jobs = getattr(self._registry, "list", None)
        view_job = getattr(self._registry, "view", None)
        if not callable(list_jobs) or not callable(view_job):
            return
        iterator = getattr(self._registry, "iter_kind", None)
        records = (
            iterator("", include_terminal=False)
            if callable(iterator)
            else list_jobs(include_terminal=False, limit=1024)
        )
        for record in records:
            view = view_job(record.identity.job_id)
            metadata = getattr(view, "metadata", None) or {}
            is_systemd_scope = metadata.get("containment_kind") == "systemd_scope"
            if is_systemd_scope:
                if not self._restore_scope_owner(record.identity.job_id, metadata):
                    self._jobs.request_cancellation(
                        record.identity.job_id,
                        "persisted systemd scope ownership is unresolved",
                        max_descendants=64,
                    )
                    self._limits[record.identity.job_id] = 64
                    self._schedule_deadline(record.identity.job_id, 0)
                    continue
            raw_deadline = metadata.get("hard_deadline_at")
            if not isinstance(raw_deadline, str) or not raw_deadline:
                if is_systemd_scope:
                    self._jobs.request_cancellation(
                        record.identity.job_id,
                        "persisted deadline safety metadata is missing",
                        max_descendants=64,
                    )
                    self._limits[record.identity.job_id] = 64
                    self._schedule_deadline(record.identity.job_id, 0)
                continue
            try:
                deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                limit = int(metadata.get("max_descendants", 64))
                if limit < 1:
                    raise ValueError
            except (TypeError, ValueError):
                if is_systemd_scope:
                    self._jobs.request_cancellation(
                        record.identity.job_id,
                        "persisted deadline safety metadata is invalid",
                        max_descendants=64,
                    )
                    self._limits[record.identity.job_id] = 64
                    self._schedule_deadline(record.identity.job_id, 0)
                else:
                    self._mark_interrupted(
                        record.identity.job_id,
                        "persisted deadline safety metadata is invalid",
                    )
                continue
            self._limits[record.identity.job_id] = limit
            if is_systemd_scope:
                self._schedule_deadline_at(record.identity.job_id, raw_deadline)
                continue
            process_identity_value = metadata.get("process_instance_identity")
            if not isinstance(process_identity_value, str) or not process_identity_value:
                self._mark_interrupted(
                    record.identity.job_id,
                    "persisted process identity is unavailable after restart",
                )
                continue
            self._schedule_deadline_at(record.identity.job_id, raw_deadline)

    def _mark_interrupted(self, job_id: str, detail: str) -> None:
        record = self._registry.transition(
            job_id,
            JobStatus.INTERRUPTED,
            error=detail,
        )
        if self._jobs._lifecycle is not None:
            self._jobs._lifecycle.record(record)

    def _discard_deadline(self, job_id: str) -> None:
        with self._timer_lock:
            timer = self._deadline_timers.pop(job_id, None)
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()

    def _quiesce_containment(
        self,
        job_id: str,
        *,
        force: bool,
    ) -> ProcessContainmentResult | None:
        token = self._memory_tokens.get(job_id)
        if token is None:
            return None
        quiesce = getattr(token, "quiesce", None)
        if not callable(quiesce):
            return None
        try:
            result = quiesce(force=force)
        except Exception as exc:
            return ProcessContainmentResult(
                False,
                forced=force,
                detail=f"process containment cleanup failed: {type(exc).__name__}",
            )
        if not isinstance(result, ProcessContainmentResult):
            return ProcessContainmentResult(
                False,
                forced=force,
                detail="process containment returned invalid cleanup proof",
            )
        return result

    def _restore_scope_owner(self, job_id: str, metadata: dict[str, Any]) -> bool:
        if job_id in self._memory_tokens:
            self._unresolved_scopes.pop(job_id, None)
            return True
        restore_scope = getattr(self._memory_limiter, "restore_process_job", None)
        if not callable(restore_scope):
            self._unresolved_scopes[job_id] = dict(metadata)
            return False
        try:
            token = restore_scope(job_id, metadata)
        except Exception:
            self._unresolved_scopes[job_id] = dict(metadata)
            return False
        if token is None:
            self._unresolved_scopes[job_id] = dict(metadata)
            return False
        self._memory_tokens[job_id] = token
        self._unresolved_scopes.pop(job_id, None)
        return True

    def _release_capacity(self, job_id: str) -> None:
        release = getattr(self._registry, "release_capacity", None)
        if callable(release):
            release(job_id)

    def _forget_local_job(self, job_id: str) -> None:
        self._release_memory_limit(job_id)
        self._release_capacity(job_id)
        self._processes.pop(job_id, None)
        self._failed_launches.discard(job_id)
        self._limits.pop(job_id, None)
        self._release_process_slot(job_id)
        self._unresolved_scopes.pop(job_id, None)
        self._output_threads.pop(job_id, None)
        self._discard_deadline(job_id)

    def _release_process_slot(self, job_id: str) -> None:
        with self._timer_lock:
            lease = self._process_slot_owners.pop(job_id, None)
        if lease is not None:
            lease.release()

    def _release_memory_limit(self, job_id: str) -> None:
        token = self._memory_tokens.get(job_id)
        if token is not None:
            token.close()
            self._memory_tokens.pop(job_id, None)

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
            reader = owned_runtime_thread(
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
    def _abort_unregistered(process: Any) -> bool:
        try:
            process.kill()
        except (AttributeError, OSError):
            pass
        try:
            exit_code = process.wait(timeout=1)
        except (AttributeError, OSError, subprocess.TimeoutExpired):
            return False
        return isinstance(exit_code, int) and not isinstance(exit_code, bool)


__all__ = ["SubprocessJobProvider"]
