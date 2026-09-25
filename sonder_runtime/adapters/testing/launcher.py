"""Launch a planned test run as a durable process job and reap it.

The run goes through the runtime's ``ProcessJobProvider``: a replacement
environment (``inherit_environment=False``), a hard deadline, a descendant and
memory bound, process-tree termination on cancel or deadline, and output
captured in the durable job registry. One reaper thread per run (from
``platform.runtime_threads``) is the only caller of ``provider.wait``, so the
job reaches its terminal state whether or not anyone polls it; callers wait
on the reaper's completion event instead.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ...application.context import OperationContext
from ...application.execution.process_jobs import ProcessJobRequest
from ...application.ports.jobs import JobIdentity, JobRecord
from ...application.testing.ports import JOB_KIND, TestRunPlan
from ...platform import runtime_threads
from ...platform.private_files import ensure_private_dir

logger = logging.getLogger(__name__)

MAX_RETAINED_RUN_DIRS = 64
_REAP_SLICE_SECONDS = 0.5
_REAP_GRACE_SECONDS = 120.0
_MAX_REAP_FAILURES = 50
_CANCEL_PROOF_SECONDS = 15.0
# Finished runs remembered in process for their exit codes; older ones fall
# back to the durable record (and the cached report).
_MAX_REMEMBERED_RUNS = 256


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class _Run:
    __slots__ = ("principal_id", "done", "exit_code", "report_dir")

    def __init__(self, principal_id: str, report_dir: str) -> None:
        self.principal_id = principal_id
        self.done = threading.Event()
        self.exit_code: int | None = None
        self.report_dir = report_dir


class ProcessTestLauncher:
    """``TestRunLauncher`` over the durable ``ProcessJobProvider``."""

    def __init__(self, provider_getter: Callable[[], Any], registry_getter: Callable[[], Any], *,
                 executable_guard: Callable[[str], str], report_root: str,
                 clock: Callable[[], float] = time.time,
                 max_retained: int = MAX_RETAINED_RUN_DIRS) -> None:
        self._provider_getter = provider_getter
        self._registry_getter = registry_getter
        self._guard = executable_guard
        self._report_root = Path(report_root)
        self._clock = clock
        self._max_retained = max_retained
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    # -- launch ----------------------------------------------------------------

    def _check_executables(self, plan: TestRunPlan) -> None:
        for path in plan.checked_executables:
            self._guard(path)  # raises PermissionError; re-checked at launch (TOCTOU)
        if plan.project_executable:
            executable = Path(plan.argv[0])
            root = Path(plan.project_root)
            lexical = Path(os.path.normpath(os.path.abspath(executable)))
            if not _inside(lexical, root) or _is_reparse(lexical.parent):
                raise PermissionError("project executable must stay inside the project")
            try:
                target = executable.resolve(strict=True)
                regular = stat.S_ISREG(target.stat().st_mode)
            except OSError:
                regular = False
            if not regular:
                raise PermissionError("project executable is not a regular file")

    def _prepare_report_dir(self, plan: TestRunPlan) -> None:
        report_dir = Path(plan.report_dir)
        if report_dir.parent != self._report_root:
            raise PermissionError("test report directory is outside the report root")
        ensure_private_dir(self._report_root)
        if _is_reparse(self._report_root):
            raise PermissionError("test report root is a symlink")
        self._prune()
        os.mkdir(report_dir, 0o700)  # exclusive: FileExistsError on reuse
        if os.name != "nt":
            os.chmod(report_dir, 0o700)

    def _prune(self) -> None:
        try:
            with os.scandir(self._report_root) as iterator:
                entries = [entry for index, entry in enumerate(iterator) if index < 4096]
        except OSError:
            return
        with self._lock:
            active = {Path(run.report_dir).name for run in self._runs.values() if not run.done.is_set()}
        candidates = []
        for entry in entries:
            if entry.name in active or not entry.is_dir(follow_symlinks=False):
                continue
            try:
                candidates.append((entry.stat(follow_symlinks=False).st_mtime, entry.path))
            except OSError:
                continue
        excess = len(candidates) - (self._max_retained - 1)
        for _, path in sorted(candidates)[: max(0, excess)]:
            try:
                shutil.rmtree(path)
            except OSError:
                logger.warning("could not prune an old test run directory", exc_info=True)

    def start(self, plan: TestRunPlan, context: OperationContext, job_id: str) -> None:
        self._check_executables(plan)
        self._prepare_report_dir(plan)
        metadata = (
            ("principal_id", context.principal_id),
            ("runner", plan.runner.value),
            ("command_digest", plan.command_digest),
            ("cwd_label", plan.cwd_label),
            ("cwd", plan.cwd),
            ("project_root", plan.project_root),
            ("report_dir", plan.report_dir),
            ("report_file", plan.report_file),
            ("report_glob", plan.report_glob),
            ("report_format", plan.report_format.value),
            ("selector", plan.selector),
            ("display_argv_json", json.dumps(list(plan.display_argv), ensure_ascii=False)[:4096]),
            ("notes_json", json.dumps(list(plan.notes), ensure_ascii=False)[:4096]),
            ("started_at", "%.3f" % self._clock()),
        )
        request = ProcessJobRequest(
            JobIdentity(job_id, JOB_KIND, context.correlation_id, job_id,
                        parent_session_id=getattr(context, "session_id", None)),
            tuple(plan.argv),
            cwd=Path(plan.cwd),
            environment=tuple(plan.environment),
            inherit_environment=False,
            require_job_scope=False,
            max_descendants=plan.max_descendants,
            deadline_seconds=plan.timeout_seconds,
            memory_limit_bytes=plan.memory_limit_bytes,
            metadata=metadata,
        )
        run = _Run(context.principal_id, plan.report_dir)
        with self._lock:
            finished = [key for key, item in self._runs.items() if item.done.is_set()]
            for key in finished[: max(0, len(finished) - _MAX_REMEMBERED_RUNS)]:
                self._runs.pop(key, None)
            self._runs[job_id] = run
        try:
            self._provider_getter().start(request)
        except BaseException:
            with self._lock:
                self._runs.pop(job_id, None)
            run.done.set()
            shutil.rmtree(plan.report_dir, ignore_errors=True)
            raise
        reaper = runtime_threads.Thread(
            target=self._reap, args=(job_id, run, plan.timeout_seconds),
            name="sonder-test-run-reaper", daemon=True,
        )
        reaper.start()

    def _reap(self, job_id: str, run: _Run, timeout_seconds: int) -> None:
        provider = self._provider_getter()
        give_up = time.monotonic() + timeout_seconds + _REAP_GRACE_SECONDS
        failures = 0
        try:
            while time.monotonic() < give_up:
                try:
                    waited = provider.wait(job_id, timeout=_REAP_SLICE_SECONDS)
                except KeyError:
                    # Cancellation or the deadline controller released the
                    # process handle; the durable record is authoritative.
                    if self._terminal(job_id):
                        return
                    failures += 1
                except Exception:
                    if self._terminal(job_id):
                        return
                    failures += 1
                    logger.warning("test run reaper wait failed", exc_info=True)
                else:
                    if not waited.timed_out:
                        run.exit_code = waited.exit_code
                        if waited.record.is_terminal or self._terminal(job_id):
                            return
                        failures += 1
                if failures:
                    if failures >= _MAX_REAP_FAILURES:
                        logger.error("test run reaper gave up; the deadline controller owns cleanup")
                        return
                    run.done.wait(0.2)
        finally:
            run.done.set()

    def _terminal(self, job_id: str) -> bool:
        record = self.poll(job_id)
        return record is not None and record.is_terminal

    # -- controls --------------------------------------------------------------

    def poll(self, job_id: str) -> JobRecord | None:
        try:
            return self._registry_getter().get(job_id)
        except KeyError:
            return None

    def wait(self, job_id: str, timeout: float) -> tuple[JobRecord, int | None, bool]:
        timeout = max(0.0, float(timeout))
        with self._lock:
            run = self._runs.get(job_id)
        if run is not None:
            run.done.wait(timeout)
        else:
            deadline = time.monotonic() + timeout
            pause = threading.Event()
            while True:
                record = self.poll(job_id)
                if record is None or record.is_terminal or time.monotonic() >= deadline:
                    break
                pause.wait(min(0.25, max(0.0, deadline - time.monotonic())))
        record = self.poll(job_id)
        if record is None:
            raise KeyError(job_id)
        exit_code = run.exit_code if run is not None else None
        if exit_code is None and isinstance(record.result, Mapping):
            value = record.result.get("exit_code")
            exit_code = value if isinstance(value, int) and not isinstance(value, bool) else None
        return record, exit_code, not record.is_terminal

    def cancel(self, job_id: str, reason: str) -> bool:
        """Cancel the run's process tree; True once cleanup is proven.

        The provider reports incomplete cleanup honestly (a root whose exit is
        not yet observed) and retries it; the job becomes terminal only when
        the tree is gone. A bounded wait for that terminal state is the proof.
        """
        result = self._provider_getter().cancel(job_id, reason=reason)
        if getattr(result, "cleanup_completed", False):
            cleaned = True
        else:
            deadline = time.monotonic() + _CANCEL_PROOF_SECONDS
            pause = threading.Event()
            cleaned = False
            while time.monotonic() < deadline:
                record = self.poll(job_id)
                if record is not None and record.is_terminal:
                    cleaned = True
                    break
                pause.wait(0.1)
        with self._lock:
            run = self._runs.get(job_id)
        if run is not None:
            run.done.wait(5.0)
        return cleaned

    def metadata(self, job_id: str) -> Mapping[str, str] | None:
        try:
            view = self._registry_getter().view(job_id)
        except KeyError:
            return None
        values = {str(key): str(value) for key, value in dict(view.metadata or {}).items()
                  if isinstance(value, (str, int, float)) and not isinstance(value, bool)}
        values["kind"] = view.record.identity.kind
        values["job_id"] = view.record.identity.job_id
        return values

    def running_for(self, principal_id: str) -> int:
        with self._lock:
            runs = [(job_id, run) for job_id, run in self._runs.items()
                    if run.principal_id == principal_id and not run.done.is_set()]
        count = 0
        for job_id, _ in runs:
            record = self.poll(job_id)
            if record is not None and not record.is_terminal:
                count += 1
        return count


__all__ = ["MAX_RETAINED_RUN_DIRS", "ProcessTestLauncher"]
