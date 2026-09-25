"""Run one planned build action as an owned durable job and report it.

The service composes the planner (host-owned argv), the build-dir leases
(one writer per build tree), the launcher (a durable process job with a hard
deadline and a private log) and the collector (the log scanned into typed
diagnostics). It creates no threads: waiting is a bounded ``launcher.wait``
in short slices, so cancelling the caller's operation cancels the job.

Leases and caps:

* one lease per build directory; a second job on a held directory gets
  ``BUILD_DIR_BUSY``;
* at most ``max_concurrent_per_principal`` top-level jobs per principal
  (``BUILD_BUSY``). A build fix reserves the build directory with
  ``reserve`` and counts as one job; its child builds run under a child of
  that lease, do not count against the cap, and are still exclusive: a fix
  runs one child build at a time and no other job can take the directory.

Ownership: every job-id method requires a ``tool.build_job`` job whose
``principal_id`` matches the caller; anything else is ``JOB_NOT_FOUND``,
indistinguishable from a job that does not exist.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Mapping

from ...domain.common.errors import InvalidInput
from ..context import OperationContext
from ..ports.jobs import JobRecord
from .model_service import BuildModelService
from .ports import (
    ACTION_CONFIGURE,
    BUILD_BUSY,
    BUILD_DIR_BUSY,
    BUILD_JOB_ID_RE,
    BUILD_JOB_KIND,
    BUILD_JOB_PREFIX,
    BUILD_TREE_MISSING,
    JOB_NOT_FOUND,
    BuildDirLease,
    BuildDirLeases,
    BuildJobPlan,
    BuildJobRequest,
    BuildJobStatusView,
    BuildLauncher,
    BuildOutputCollector,
    BuildPlanner,
    build_error,
)

MAX_RUN_WAIT_SECONDS = 120
MAX_RESULT_WAIT_SECONDS = 120
_WAIT_SLICE_SECONDS = 1.0
_MAX_REASON_CHARS = 120


def _not_found() -> Exception:
    return build_error(JOB_NOT_FOUND, "build job not found")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_dir(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _one_line(text: str, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


class InMemoryBuildDirLeases:
    """``BuildDirLeases``: one holder per build directory, in this process.

    ``is_active(owner_job_id)`` lets a lease whose job is no longer running
    (a crash between launch and release) be reclaimed instead of wedging the
    directory until restart.
    """

    def __init__(self, *, is_active: Callable[[str], bool] | None = None,
                 normalize: Callable[[str], str] | None = None) -> None:
        self._is_active = is_active
        # The default folds case and separators the way the host's file system
        # does, so ``C:\\B`` and ``c:/b/`` are one lease on Windows.
        self._normalize = normalize or _normalize_dir
        self._holders: dict[str, BuildDirLease] = {}
        self._children: dict[str, BuildDirLease] = {}
        self._lock = threading.Lock()

    def _key(self, build_dir: str) -> str:
        if not isinstance(build_dir, str) or not build_dir:
            raise InvalidInput("build_dir must be a non-empty path")
        return self._normalize(build_dir)

    def _stale(self, lease: BuildDirLease) -> bool:
        if self._is_active is None:
            return False
        try:
            return not self._is_active(lease.owner_job_id)
        except Exception:
            return False

    def acquire(self, build_dir: str, owner_job_id: str, principal_id: str) -> BuildDirLease:
        key = self._key(build_dir)
        with self._lock:
            holder = self._holders.get(key)
            if holder is not None and self._stale(holder):
                self._holders.pop(key, None)
                self._children.pop(holder.lease_id, None)
                holder = None
            if holder is not None:
                raise build_error(BUILD_DIR_BUSY, "another build job holds this build directory")
            lease = BuildDirLease(uuid.uuid4().hex, key, owner_job_id, principal_id)
            self._holders[key] = lease
            return lease

    def child(self, lease: BuildDirLease, owner_job_id: str) -> BuildDirLease:
        if not isinstance(lease, BuildDirLease) or lease.is_child:
            raise InvalidInput("a child lease needs a held top-level lease")
        with self._lock:
            if self._holders.get(lease.build_dir) != lease:
                raise build_error(BUILD_DIR_BUSY, "the parent lease no longer holds the build directory")
            current = self._children.get(lease.lease_id)
            if current is not None and self._stale(current):
                self._children.pop(lease.lease_id, None)
                current = None
            if current is not None:
                raise build_error(BUILD_DIR_BUSY, "a child build already runs in this build directory")
            child = BuildDirLease(uuid.uuid4().hex, lease.build_dir, owner_job_id,
                                  lease.principal_id, parent_lease_id=lease.lease_id)
            self._children[lease.lease_id] = child
            return child

    def release(self, lease: BuildDirLease) -> None:
        if not isinstance(lease, BuildDirLease):
            return
        with self._lock:
            if lease.is_child:
                if self._children.get(lease.parent_lease_id) == lease:
                    self._children.pop(lease.parent_lease_id, None)
                return
            if self._holders.get(lease.build_dir) == lease:
                self._holders.pop(lease.build_dir, None)
                self._children.pop(lease.lease_id, None)

    def holder(self, build_dir: str) -> BuildDirLease | None:
        with self._lock:
            return self._holders.get(self._key(build_dir))


class BuildJobService:
    def __init__(self, planner: BuildPlanner, launcher: BuildLauncher,
                 collector: BuildOutputCollector, model_service: BuildModelService,
                 leases: BuildDirLeases, *, clock: Callable[[], float],
                 max_concurrent_per_principal: int = 2) -> None:
        if isinstance(max_concurrent_per_principal, bool) or max_concurrent_per_principal < 1:
            raise ValueError("max_concurrent_per_principal must be positive")
        self._planner = planner
        self._launcher = launcher
        self._collector = collector
        self._models = model_service
        self._leases = leases
        self._clock = clock
        self._max_concurrent = max_concurrent_per_principal
        self._start_lock = threading.Lock()
        # Top-level reservations held by build fixes: lease_id -> lease.
        self._reservations: dict[str, BuildDirLease] = {}

    # -- planning --------------------------------------------------------------

    def model_for(self, request: BuildJobRequest, context: OperationContext) -> Any | None:
        """The model a job plans against; None only for a configure without a tree."""
        try:
            return self._models.model(request.model_request(), context)
        except Exception as exc:
            if request.action == ACTION_CONFIGURE and getattr(exc, "code", "") == BUILD_TREE_MISSING:
                return None
            raise

    def plan(self, request: BuildJobRequest, context: OperationContext, *,
             lease: BuildDirLease | None = None) -> BuildJobPlan:
        if not isinstance(request, BuildJobRequest):
            raise InvalidInput("request must be a BuildJobRequest")
        model = self.model_for(request, context)
        return self._planner.plan_run(request, model, context,
                                      lease=lease.lease_id if lease is not None else None)

    # -- leases for build fixes ------------------------------------------------

    def reserve(self, build_dir: str, owner_job_id: str, context: OperationContext) -> BuildDirLease:
        """Hold ``build_dir`` for a build fix; counts as one job of the principal."""
        with self._start_lock:
            self._check_cap(context.principal_id)
            lease = self._leases.acquire(build_dir, owner_job_id, context.principal_id)
            self._reservations[lease.lease_id] = lease
            return lease

    def release(self, lease: BuildDirLease) -> None:
        with self._start_lock:
            self._reservations.pop(getattr(lease, "lease_id", ""), None)
        self._leases.release(lease)

    def _active_count(self, principal_id: str) -> int:
        reserved = sum(1 for lease in self._reservations.values() if lease.principal_id == principal_id)
        return self._launcher.running_for(principal_id) + reserved

    def _check_cap(self, principal_id: str) -> None:
        if self._active_count(principal_id) >= self._max_concurrent:
            raise build_error(BUILD_BUSY, "at most %d build jobs may run at once per caller"
                              % self._max_concurrent)

    # -- launch ----------------------------------------------------------------

    def start(self, request: BuildJobRequest, context: OperationContext, *,
              plan: BuildJobPlan | None = None, lease: BuildDirLease | None = None,
              parent_job_id: str = "") -> str:
        """Launch one job; ``lease`` is a build fix's reservation for a child build."""
        if context.expired or context.cancellation.cancelled:
            raise InvalidInput("the operation was cancelled or expired before the build started")
        plan = plan if plan is not None else self.plan(request, context, lease=lease)
        token = plan.run_token or uuid.uuid4().hex
        job_id = BUILD_JOB_PREFIX + token
        if not BUILD_JOB_ID_RE.fullmatch(job_id):
            raise InvalidInput("the plan carries an invalid run token")
        # The cap check, the lease and the launch are one step: two concurrent
        # calls by one caller must not both pass the check before either starts.
        with self._start_lock:
            if lease is not None:
                if lease.principal_id != context.principal_id or lease.lease_id not in self._reservations:
                    raise build_error(BUILD_DIR_BUSY, "the build fix lease is not held by this caller")
                if self._leases.holder(plan.build_dir) != lease:
                    raise build_error(BUILD_DIR_BUSY, "the build fix lease does not cover this build directory")
                held = self._leases.child(lease, job_id)
            else:
                self._check_cap(context.principal_id)
                held = self._leases.acquire(plan.build_dir, job_id, context.principal_id)

            def release(_job_id: str, held: BuildDirLease = held) -> None:
                self._leases.release(held)

            try:
                self._launcher.start(plan, context, job_id, parent_job_id=parent_job_id,
                                     on_exit=release)
            except BaseException:
                self._leases.release(held)
                raise
        if plan.action == ACTION_CONFIGURE:
            # A configure rewrites the tree; the next model read must not hit
            # a cache entry keyed by the old fingerprint's TTL.
            self._models.invalidate(context.principal_id, plan.project_root, plan.build_dir)
        return job_id

    def run(self, request: BuildJobRequest, context: OperationContext, *, wait_seconds: float,
            plan: BuildJobPlan | None = None, lease: BuildDirLease | None = None,
            parent_job_id: str = "") -> Any:
        job_id = self.start(request, context, plan=plan, lease=lease, parent_job_id=parent_job_id)
        wait = max(0.0, min(float(MAX_RUN_WAIT_SECONDS), float(wait_seconds or 0)))
        return self._await(job_id, context, wait, cancel_on_abort=True)

    # -- job controls ----------------------------------------------------------

    def status(self, job_id: str, context: OperationContext) -> BuildJobStatusView:
        meta = self._owned(job_id, context)
        record = self._launcher.poll(job_id)
        if record is None:
            raise _not_found()
        return self._status_view(job_id, record, meta)

    def result(self, job_id: str, context: OperationContext, *, wait_seconds: float = 0) -> Any:
        self._owned(job_id, context)
        wait = max(0.0, min(float(MAX_RESULT_WAIT_SECONDS), float(wait_seconds or 0)))
        return self._await(job_id, context, wait, cancel_on_abort=False)

    def cancel(self, job_id: str, context: OperationContext, *, reason: str = "cancelled") -> BuildJobStatusView:
        meta = self._owned(job_id, context)
        record = self._launcher.poll(job_id)
        cleaned: bool | None = None
        if record is not None and not record.is_terminal:
            cleaned = bool(self._launcher.cancel(job_id, _one_line(reason or "cancelled", _MAX_REASON_CHARS)))
            record = self._launcher.poll(job_id) or record
        if record is None:
            raise _not_found()
        return self._status_view(job_id, record, meta, cleanup_proven=cleaned)

    # -- internals -------------------------------------------------------------

    def _owned(self, job_id: str, context: OperationContext) -> Mapping[str, str]:
        if not isinstance(job_id, str) or not BUILD_JOB_ID_RE.fullmatch(job_id):
            raise _not_found()
        meta = self._launcher.metadata(job_id)
        if (
            meta is None
            or meta.get("kind") != BUILD_JOB_KIND
            or meta.get("principal_id") != context.principal_id
        ):
            raise _not_found()
        return meta

    def _await(self, job_id: str, context: OperationContext, wait: float, *,
               cancel_on_abort: bool) -> Any:
        meta = self._launcher.metadata(job_id) or {}
        remaining = context.remaining_seconds
        if remaining is not None:
            # Leave the caller time to render; never wait past its deadline.
            wait = max(0.0, min(wait, remaining - 1.0))
        record, exit_code, _ = self._launcher.wait(job_id, 0)
        deadline = self._clock() + wait
        while not record.is_terminal:
            if context.cancellation.cancelled or context.expired:
                if cancel_on_abort:
                    self._launcher.cancel(job_id, "caller operation cancelled")
                    record, exit_code, _ = self._launcher.wait(job_id, 5.0)
                    if record.is_terminal:
                        break
                return self._status_view(job_id, record, meta)
            left = deadline - self._clock()
            if left <= 0:
                return self._status_view(job_id, record, meta)
            record, exit_code, _ = self._launcher.wait(job_id, min(_WAIT_SLICE_SECONDS, left))
        model = self._models.cached_model(context.principal_id, str(meta.get("project_root", "")),
                                          str(meta.get("build_dir", "")))
        return self._collector.collect(job_id, meta, model, record=record, exit_code=exit_code)

    def _status_view(self, job_id: str, record: JobRecord, meta: Mapping[str, str], *,
                     cleanup_proven: bool | None = None) -> BuildJobStatusView:
        started = _float(meta.get("started_at"), self._clock())
        status = record.status.value
        if record.is_terminal and status == "cancelled" and "deadline" in (record.error or "").lower():
            status = "timed_out"
        return BuildJobStatusView(
            job_id=job_id,
            status=status,
            action=str(meta.get("action", "")),
            system=str(meta.get("system", "")),
            elapsed_seconds=max(0.0, self._clock() - started),
            command_digest=str(meta.get("command_digest", "")),
            display_command=self._display(meta),
            parent_job_id=str(meta.get("parent_job_id", "")),
            cleanup_proven=cleanup_proven,
        )

    @staticmethod
    def _display(meta: Mapping[str, str]) -> tuple[str, ...]:
        try:
            values = json.loads(meta.get("display_argv_json", "[]"))
        except ValueError:
            return ()
        return tuple(str(item) for item in values) if isinstance(values, list) else ()


def build_job_liveness(launcher: Any) -> Callable[[str], bool]:
    """``is_active`` for ``InMemoryBuildDirLeases``: only build jobs can go stale.

    A build fix's reservation is owned by the fix job, which releases it
    explicitly; it is always treated as live here.
    """
    def is_active(owner_job_id: str) -> bool:
        if not str(owner_job_id).startswith(BUILD_JOB_PREFIX):
            return True
        check = getattr(launcher, "is_active", None)
        return bool(check(owner_job_id)) if callable(check) else True

    return is_active


def epoch_from_iso(text: str) -> float | None:
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


__all__ = [
    "BuildJobService", "InMemoryBuildDirLeases", "MAX_RESULT_WAIT_SECONDS",
    "MAX_RUN_WAIT_SECONDS", "build_job_liveness", "epoch_from_iso",
]
