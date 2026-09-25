"""Run a debug plan as a chain of durable process jobs in a private run directory.

The -3 ``ProcessTestLauncher`` patterns, applied to debuggers:

- every step goes through the runtime's ``ProcessJobProvider`` with a
  replacement environment (``inherit_environment=False``), a hard deadline,
  a descendant bound of 8, a memory bound and process-tree termination on
  cancel or deadline; the executable guard is re-run at launch (TOCTOU);
- one reaper thread per run (``platform.runtime_threads``) drives the chain
  and is the only caller of ``provider.wait``; callers wait on its event;
- run directories are exclusive and private (0700) under
  ``state/debug-runs`` and pruned to the newest 32.

Added for hostile captures:

- the nonce (``secrets.token_hex(8)``) and the run directory are bound here,
  at start, never in the plan (approval digests stay stable);
- step ``i`` is job ``<run_id>-s<i>`` with metadata ``run_id``, ``step``,
  ``principal_id``, ``command_digest`` and ``input_sha256``;
- an output watchdog: the job registry keeps only a bounded tail of output,
  so the launcher counts what each step printed (0.5 s polls) and cancels
  the step past 16 MiB (``OUTPUT_LIMIT``, partial result);
- after the chain, a capture that was not copied is re-checked
  (``INPUT_CHANGED`` when it changed), then staged inputs, symbol staging,
  tool caches, HOME and TMP are deleted. The run directory keeps only its
  small JSON records (plan, chain state, result).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from ...application.context import OperationContext
from ...application.debugging.ports import (
    CRASH_JOB_KIND,
    PROFILE_JOB_KIND,
    DebugPlan,
    DebugRunState,
)
from ...application.execution.process_jobs import ProcessJobRequest
from ...application.execution.world_control import OutputWatermark
from ...application.ports.jobs import JobIdentity, JobStatus
from ...domain.common.errors import SonderError
from ...platform import runtime_threads
from ...platform.private_files import ensure_private_dir
from .capture_source import share_deny_handle

logger = logging.getLogger(__name__)

RUN_ROOT_NAME = "debug-runs"
MAX_RETAINED_RUN_DIRS = 32
OUTPUT_LIMIT_BYTES = 16 * 1024 * 1024
MAX_DESCENDANTS = 8
WATCHDOG_SECONDS = 0.5
FILE_OUTPUT_MAX_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
_CANCEL_PROOF_SECONDS = 15.0
_MAX_REMEMBERED_RUNS = 128
_RUN_ID = re.compile(r"^debug-run-[0-9a-f]{32}$")
_JSON_NAMES = frozenset({"plan.json", "context.json", "result.json", "chain.json"})
_KEEP = frozenset({"plan.json", "context.json", "result.json", "chain.json"})
_STEP_OUTPUT = re.compile(r"^step-\d{1,2}\.out$")
_BASE_DIRS = ("cwd", "home", "tmp", "in")


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _safe_relative(rel: str) -> PurePosixPath:
    path = PurePosixPath(str(rel).replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise PermissionError("run directory entries must be plain relative paths")
    return path


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp-%s" % secrets.token_hex(4))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) \
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class _Run:
    __slots__ = ("run_id", "principal_id", "plan", "rundir", "nonce", "done", "cancel_requested",
                 "cancel_reason", "current_job", "step_status", "exit_codes", "output_limit",
                 "input_changed", "status", "notes", "step_outputs", "started_at", "finished_at",
                 "kind", "stack", "staged_input", "correlation_id", "session_id")

    def __init__(self, run_id: str, principal_id: str, plan: DebugPlan, rundir: Path,
                 nonce: str, started_at: float) -> None:
        self.run_id = run_id
        self.principal_id = principal_id
        self.plan = plan
        self.rundir = rundir
        self.nonce = nonce
        self.done = threading.Event()
        self.cancel_requested = False
        self.cancel_reason = ""
        self.current_job: str | None = None
        self.step_status: list[str] = []
        self.exit_codes: list[int | None] = []
        self.output_limit = False
        self.input_changed = False
        self.status = "running"
        self.notes: list[str] = []
        self.step_outputs: dict[int, str] = {}
        self.started_at = started_at
        self.finished_at: float | None = None
        self.kind = CRASH_JOB_KIND if plan.kind == "crash" else PROFILE_JOB_KIND
        self.stack = contextlib.ExitStack()
        self.staged_input = ""
        self.correlation_id = ""
        self.session_id: str | None = None


class ProcessDebugLauncher:
    """``DebugLauncher`` over the durable ``ProcessJobProvider``."""

    def __init__(self, provider_getter: Callable[[], Any], registry_getter: Callable[[], Any], *,
                 executable_guard: Callable[[str], str], run_root: str, source: Any,
                 clock: Callable[[], float] = time.time,
                 max_retained: int = MAX_RETAINED_RUN_DIRS,
                 output_limit_bytes: int = OUTPUT_LIMIT_BYTES,
                 watchdog_seconds: float = WATCHDOG_SECONDS) -> None:
        self._provider_getter = provider_getter
        self._registry_getter = registry_getter
        self._guard = executable_guard
        self._root = Path(run_root)
        self._source = source
        self._clock = clock
        self._max_retained = max(2, int(max_retained))
        self._output_limit = int(output_limit_bytes)
        self._watchdog = float(watchdog_seconds)
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    # -- run directories -------------------------------------------------------

    def _rundir(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(str(run_id)):
            raise KeyError(run_id)
        return self._root / run_id

    def _prepare(self, run_id: str, plan: DebugPlan) -> Path:
        ensure_private_dir(self._root)
        if _is_reparse(self._root):
            raise PermissionError("debug run root is a symlink")
        if os.name != "nt":
            os.chmod(self._root, 0o700)
        self._prune()
        rundir = self._rundir(run_id)
        os.mkdir(rundir, 0o700)  # exclusive: FileExistsError on reuse
        if os.name != "nt":
            os.chmod(rundir, 0o700)
        for rel in _BASE_DIRS + tuple(plan.mkdirs):
            target = rundir.joinpath(*_safe_relative(rel).parts)
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        return rundir

    def _prune(self) -> None:
        try:
            with os.scandir(self._root) as iterator:
                entries = [entry for index, entry in enumerate(iterator) if index < 4096]
        except OSError:
            return
        with self._lock:
            active = {run_id for run_id, run in self._runs.items() if not run.done.is_set()}
        candidates = []
        for entry in entries:
            if entry.name in active or not entry.is_dir(follow_symlinks=False):
                continue
            if not _RUN_ID.fullmatch(entry.name):
                continue
            try:
                candidates.append((entry.stat(follow_symlinks=False).st_mtime, entry.path))
            except OSError:
                continue
            # An orphan of a crashed process keeps no staged capture or cache.
            self._scrub(Path(entry.path), keep_step_outputs=True)
        excess = len(candidates) - (self._max_retained - 1)
        for _, path in sorted(candidates)[: max(0, excess)]:
            shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _scrub(rundir: Path, *, keep_step_outputs: bool) -> None:
        try:
            with os.scandir(rundir) as iterator:
                entries = list(iterator)[:256]
        except OSError:
            return
        for entry in entries:
            if entry.name in _KEEP or (keep_step_outputs and _STEP_OUTPUT.match(entry.name)):
                continue
            path = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink()
            except OSError:
                logger.warning("could not remove a debug run staging entry", exc_info=True)

    # -- launch ----------------------------------------------------------------

    def _check_executables(self, plan: DebugPlan) -> None:
        for path in plan.checked_executables:
            self._guard(path)  # raises PermissionError; re-checked per step too

    def start(self, plan: DebugPlan, context: OperationContext, run_id: str) -> DebugRunState:
        if not plan.steps:
            raise ValueError("a plan without steps is not launched")
        self._check_executables(plan)
        rundir = self._prepare(run_id, plan)
        run = _Run(run_id, context.principal_id, plan, rundir, secrets.token_hex(8), self._clock())
        run.correlation_id = context.correlation_id
        run.session_id = getattr(context, "session_id", None)
        try:
            if os.name == "nt" and plan.staging != "copy":
                run.stack.enter_context(share_deny_handle(plan.input_identity.path))
            run.staged_input = self._source.stage(plan.input_identity, str(rundir),
                                                  strategy=plan.staging)
            for source_path, rel in plan.staged_files:
                dest = rundir.joinpath(*_safe_relative(rel).parts)
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self._source.stage_file(source_path, str(dest))
            self._write_json(rundir / "plan.json", self._plan_record(run))
        except BaseException:
            run.stack.close()
            shutil.rmtree(rundir, ignore_errors=True)
            raise
        with self._lock:
            finished = [key for key, item in self._runs.items() if item.done.is_set()]
            for key in finished[: max(0, len(finished) - _MAX_REMEMBERED_RUNS)]:
                self._runs.pop(key, None)
            self._runs[run_id] = run
        reaper = runtime_threads.Thread(target=self._chain, args=(run,),
                                        name="sonder-debug-run-reaper", daemon=True)
        try:
            reaper.start()
        except BaseException:
            with self._lock:
                self._runs.pop(run_id, None)
            run.stack.close()
            shutil.rmtree(rundir, ignore_errors=True)
            raise
        return self._state(run)

    def _plan_record(self, run: _Run) -> dict:
        plan = run.plan
        return {
            "run_id": run.run_id, "principal_id": run.principal_id, "kind": run.kind,
            "plan_kind": plan.kind, "source_kind": plan.source_kind,
            "input_label": plan.input_label, "input_sha256": plan.input_sha256,
            "command_digest": plan.command_digest, "network": bool(plan.network),
            "stores_display": list(plan.stores_display), "engines": list(plan.engines),
            "egress_isolation": plan.egress_isolation, "staging": plan.staging,
            "nonce": run.nonce, "started_at": run.started_at,
            "notes": list(plan.notes)[:32],
            "module_symbols": [list(item) for item in plan.module_symbols],
            "verified_modules": list(plan.verified_modules),
            "step_job_ids": ["%s-s%d" % (run.run_id, index) for index in range(len(plan.steps))],
            "steps": [{"engine": step.engine, "parser": step.parser,
                       "display_argv": list(step.display_argv),
                       "reads_output_via": step.reads_output_via,
                       "isolation": step.isolation} for step in plan.steps],
        }

    # -- the chain ---------------------------------------------------------------

    def _bindings(self, run: _Run) -> dict[str, str]:
        rundir = str(run.rundir)
        values = {"nonce": run.nonce, "rundir": rundir, "input": run.staged_input}
        for key, value in run.plan.bindings:
            values[key] = str(value).replace("{rundir}", rundir)
        return values

    def _materialize(self, run: _Run, step) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        from ...domain.debugging.templates import ArgvTemplate, materialize

        template = ArgvTemplate(step.engine, tuple(step.template_argv), tuple(step.display_argv),
                                tuple(step.environment), step.parser)
        wanted = set(template.placeholders())
        bindings = {key: value for key, value in self._bindings(run).items() if key in wanted}
        return materialize(template, bindings)

    def _chain(self, run: _Run) -> None:
        try:
            for index, step in enumerate(run.plan.steps):
                if run.cancel_requested:
                    run.status = "cancelled"
                    break
                outcome = self._run_step(run, index, step)
                if outcome in ("cancelled", "timed_out", "output_limit", "start_failed"):
                    break
            self._finish(run)
        except Exception:
            logger.error("debug run chain failed", exc_info=True)
            run.status = "failed"
            run.notes.append("the run failed on the host")
            try:
                self._finish(run)
            except Exception:
                logger.error("debug run cleanup failed", exc_info=True)
        finally:
            run.done.set()

    def _run_step(self, run: _Run, index: int, step) -> str:
        job_id = "%s-s%d" % (run.run_id, index)
        try:
            self._guard(step.template_argv[0])
            argv, environment = self._materialize(run, step)
        except (PermissionError, SonderError, ValueError, KeyError) as exc:
            run.step_status.append("start_failed")
            run.exit_codes.append(None)
            run.notes.append("step %d refused at launch: %s" % (index, type(exc).__name__))
            run.status = "failed"
            return "start_failed"
        metadata = (
            ("principal_id", run.principal_id),
            ("run_id", run.run_id),
            ("step", str(index)),
            ("engine", step.engine),
            ("parser", step.parser),
            ("command_digest", run.plan.command_digest),
            ("input_sha256", run.plan.input_sha256[:128]),
            ("nonce", run.nonce),
            ("started_at", "%.3f" % self._clock()),
        )
        request = ProcessJobRequest(
            JobIdentity(job_id, run.kind, run.correlation_id or run.run_id, job_id,
                        parent_session_id=run.session_id),
            tuple(argv),
            cwd=run.rundir / "cwd",
            environment=tuple(environment),
            inherit_environment=False,
            require_job_scope=False,
            max_descendants=MAX_DESCENDANTS,
            deadline_seconds=int(step.timeout_seconds),
            memory_limit_bytes=int(step.memory_limit_bytes),
            metadata=metadata,
        )
        provider = self._provider_getter()
        run.current_job = job_id
        try:
            provider.start(request)
        except Exception as exc:
            run.step_status.append("start_failed")
            run.exit_codes.append(None)
            run.notes.append("step %d could not start: %s" % (index, type(exc).__name__))
            run.status = "failed"
            run.current_job = None
            return "start_failed"
        limit = min(self._output_limit, int(step.max_output_bytes or self._output_limit))
        counter = _OutputCounter()
        exit_code: int | None = None
        cancelled_by_us = False
        give_up = time.monotonic() + int(step.timeout_seconds) + 120.0
        while True:
            try:
                waited = provider.wait(job_id, timeout=self._watchdog)
            except KeyError:
                waited = None
            except Exception:
                logger.warning("debug step wait failed", exc_info=True)
                waited = None
            if waited is not None and not waited.timed_out:
                exit_code = waited.exit_code
            record = self._record(job_id)
            if record is not None and record.is_terminal:
                break
            if not cancelled_by_us:
                if counter.update(self._registry_getter(), job_id) > limit:
                    run.output_limit = True
                    cancelled_by_us = True
                    self._cancel_job(provider, job_id, "OUTPUT_LIMIT: step output exceeded %d bytes" % limit)
                elif run.cancel_requested:
                    cancelled_by_us = True
                    self._cancel_job(provider, job_id, run.cancel_reason or "cancelled")
            if time.monotonic() > give_up:
                self._cancel_job(provider, job_id, "debug step reaper deadline")
                run.notes.append("step %d did not finish; its process tree was cancelled" % index)
                break
            if waited is None:
                run.done.wait(0.1)
        record = self._record(job_id)
        run.current_job = None
        if exit_code is None and record is not None and isinstance(record.result, Mapping):
            value = record.result.get("exit_code")
            exit_code = value if isinstance(value, int) and not isinstance(value, bool) else None
        run.exit_codes.append(exit_code)
        status = self._step_outcome(record, exit_code, run)
        run.step_status.append(status)
        if step.reads_output_via != "argv" and status == "succeeded":
            text = self._file_output(run, step.reads_output_via)
            if text is not None:
                run.step_outputs[index] = text
                try:
                    _write_private(run.rundir / ("step-%d.out" % index), text.encode("utf-8"))
                except OSError:
                    pass
        if status in ("cancelled", "timed_out", "output_limit"):
            return status
        return status

    @staticmethod
    def _cancel_job(provider, job_id: str, reason: str) -> None:
        try:
            provider.cancel(job_id, reason=reason)
        except Exception:
            logger.warning("debug step cancel failed", exc_info=True)

    def _step_outcome(self, record, exit_code: int | None, run: _Run) -> str:
        if run.output_limit:
            return "output_limit"
        if record is None:
            return "failed"
        if record.status is JobStatus.CANCELLED:
            if "deadline" in (record.error or "").lower():
                return "timed_out"
            return "cancelled"
        if record.status is JobStatus.SUCCEEDED and (exit_code in (0, None)):
            return "succeeded"
        return "failed"

    def _file_output(self, run: _Run, via: str) -> str | None:
        kind, _, rel = str(via).partition(":")
        rel = rel.replace("{rundir}", "").lstrip("\\/")
        try:
            target = run.rundir.joinpath(*_safe_relative(rel).parts)
        except PermissionError:
            return None
        if kind == "dir":
            try:
                names = sorted(name for name in os.listdir(target) if name.lower().endswith(".csv"))
            except OSError:
                return None
            if not names:
                return None
            target = target / names[0]
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags)
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            data = os.read(fd, FILE_OUTPUT_MAX_BYTES)
        finally:
            os.close(fd)
        return data.decode("utf-8", "replace")

    def _finish(self, run: _Run) -> None:
        plan = run.plan
        if plan.staging != "copy":
            current = self._source.current(plan.input_identity)
            if current is None or not current.same_file(plan.input_identity):
                run.input_changed = True
        run.stack.close()
        self._scrub(run.rundir, keep_step_outputs=True)
        statuses = run.step_status
        if run.status == "failed" and not statuses:
            pass
        elif run.output_limit:
            run.status = "partial"
        elif run.cancel_requested or "cancelled" in statuses:
            run.status = "timed_out" if "timed_out" in statuses else "cancelled"
        elif "timed_out" in statuses:
            run.status = "timed_out"
        elif statuses and all(item == "succeeded" for item in statuses):
            run.status = "complete"
        else:
            run.status = "failed"
        run.finished_at = self._clock()
        try:
            self._write_json(run.rundir / "chain.json", self._chain_record(run))
        except OSError:
            logger.warning("could not record a debug run's final state", exc_info=True)

    def _chain_record(self, run: _Run) -> dict:
        return {
            "status": run.status, "step_status": list(run.step_status),
            "exit_codes": list(run.exit_codes), "output_limit": run.output_limit,
            "input_changed": run.input_changed, "finished_at": run.finished_at,
            "notes": list(run.notes)[:32],
        }

    def _record(self, job_id: str):
        try:
            return self._registry_getter().get(job_id)
        except KeyError:
            return None

    # -- state -------------------------------------------------------------------

    def _state(self, run: _Run) -> DebugRunState:
        plan = run.plan
        return DebugRunState(
            run_id=run.run_id, principal_id=run.principal_id, kind=run.kind,
            status=run.status if run.done.is_set() else "running",
            step_job_ids=tuple("%s-s%d" % (run.run_id, i) for i in range(len(plan.steps))),
            step_status=tuple(run.step_status), step_exit_codes=tuple(run.exit_codes),
            nonce=run.nonce, input_changed=run.input_changed, output_limit=run.output_limit,
            staging=plan.staging, egress_isolation=plan.egress_isolation,
            command_digest=plan.command_digest, input_sha256=plan.input_sha256,
            started_at=run.started_at, finished_at=run.finished_at, notes=tuple(run.notes),
        )

    def _durable_state(self, run_id: str) -> DebugRunState | None:
        plan = self.load_json(run_id, "plan.json")
        if plan is None:
            return None
        chain = self.load_json(run_id, "chain.json")
        if chain is None:
            # Neither running here nor finished: the process that owned the
            # chain is gone. Its jobs were reconciled by the registry.
            chain = {"status": "failed", "notes": ["the run was interrupted by a restart"]}
        return DebugRunState(
            run_id=run_id, principal_id=str(plan.get("principal_id", "")),
            kind=str(plan.get("kind", "")), status=str(chain.get("status", "failed")),
            step_job_ids=tuple(str(item) for item in plan.get("step_job_ids", ())),
            step_status=tuple(str(item) for item in chain.get("step_status", ())),
            step_exit_codes=tuple(item if isinstance(item, int) else None
                                  for item in chain.get("exit_codes", ())),
            nonce=str(plan.get("nonce", "")), input_changed=bool(chain.get("input_changed")),
            output_limit=bool(chain.get("output_limit")), staging=str(plan.get("staging", "")),
            egress_isolation=str(plan.get("egress_isolation", "n/a")),
            command_digest=str(plan.get("command_digest", "")),
            input_sha256=str(plan.get("input_sha256", "")),
            started_at=float(plan.get("started_at") or 0.0),
            finished_at=chain.get("finished_at") if isinstance(chain.get("finished_at"), (int, float)) else None,
            notes=tuple(str(item) for item in chain.get("notes", ()) if isinstance(item, str)),
        )

    def wait(self, run_id: str, timeout: float) -> tuple[DebugRunState, bool]:
        with self._lock:
            run = self._runs.get(run_id)
        if run is not None:
            run.done.wait(max(0.0, float(timeout)))
            return self._state(run), not run.done.is_set()
        state = self._durable_state(run_id)
        if state is None:
            raise KeyError(run_id)
        return state, False

    def cancel(self, run_id: str, reason: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            return True
        run.cancel_reason = str(reason or "cancelled")[:200]
        run.cancel_requested = True
        job_id = run.current_job
        if job_id is not None:
            self._cancel_job(self._provider_getter(), job_id, run.cancel_reason)
        return run.done.wait(_CANCEL_PROOF_SECONDS)

    def metadata(self, run_id: str) -> Mapping[str, str] | None:
        with self._lock:
            run = self._runs.get(run_id)
        if run is not None:
            return {"run_id": run_id, "principal_id": run.principal_id, "kind": run.kind,
                    "command_digest": run.plan.command_digest}
        try:
            plan = self.load_json(run_id, "plan.json")
        except KeyError:
            return None
        if plan is None:
            return None
        return {"run_id": run_id, "principal_id": str(plan.get("principal_id", "")),
                "kind": str(plan.get("kind", "")),
                "command_digest": str(plan.get("command_digest", ""))}

    def running_for(self, principal_id: str) -> int:
        with self._lock:
            return sum(1 for run in self._runs.values()
                       if run.principal_id == principal_id and not run.done.is_set())

    def step_job_ids(self, run_id: str) -> tuple[str, ...]:
        state, _ = self.wait(run_id, 0)
        return state.step_job_ids

    def step_output(self, run_id: str, index: int) -> str | None:
        with self._lock:
            run = self._runs.get(run_id)
        if run is not None and index in run.step_outputs:
            return run.step_outputs[index]
        path = self._rundir(run_id) / ("step-%d.out" % int(index))
        data = self._read_bounded(path)
        return data.decode("utf-8", "replace") if data is not None else None

    # -- small private JSON records ----------------------------------------------

    def _write_json(self, path: Path, payload: Mapping) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        _write_private(path, data.encode("utf-8"))

    @staticmethod
    def _read_bounded(path: Path) -> bytes | None:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
                return None
            return os.read(fd, MAX_JSON_BYTES)
        finally:
            os.close(fd)

    def store_json(self, run_id: str, name: str, payload: Mapping) -> None:
        if name not in _JSON_NAMES:
            raise ValueError("unknown run record %r" % name)
        rundir = self._rundir(run_id)
        if not rundir.is_dir() or _is_reparse(rundir):
            raise OSError("debug run directory is gone")
        self._write_json(rundir / name, payload)
        if name == "result.json":
            # The result is final: drop the assembly inputs, keep identity records.
            for extra in ("context.json",):
                with contextlib.suppress(OSError):
                    (rundir / extra).unlink()
            self._scrub(rundir, keep_step_outputs=False)

    def load_json(self, run_id: str, name: str) -> Mapping | None:
        if name not in _JSON_NAMES:
            raise ValueError("unknown run record %r" % name)
        data = self._read_bounded(self._rundir(run_id) / name)
        if data is None:
            return None
        try:
            value = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            return None
        return value if isinstance(value, dict) else None


class _OutputCounter:
    """Bytes a step printed, estimated from the registry's bounded retention.

    The registry keeps only a tail of each job's output, so events can be
    dropped between two polls; each dropped event is counted at the average
    size of the events seen. Spilled events count their full spilled size.
    """

    def __init__(self) -> None:
        self.cursor = OutputWatermark(0)
        self.bytes = 0
        self.events = 0

    def update(self, registry, job_id: str) -> int:
        stream = getattr(registry, "stream", None)
        if not callable(stream):
            return self.bytes
        for _ in range(64):
            try:
                page = stream(job_id, after=self.cursor, max_events=256, max_bytes=1 << 20)
            except (KeyError, ValueError):
                return self.bytes
            if not page.events:
                break
            first = page.events[0].watermark.sequence
            dropped = max(0, first - self.cursor.sequence - 1)
            sizes = []
            for event in page.events:
                spill = getattr(event, "spill", None)
                size = int(getattr(spill, "size", 0) or 0) if spill is not None else 0
                sizes.append(max(size, len(event.data.encode("utf-8", "replace"))))
            self.events += len(sizes)
            self.bytes += sum(sizes)
            if dropped:
                average = max(1, self.bytes // max(1, self.events))
                self.bytes += dropped * average
                self.events += dropped
            self.cursor = page.next_watermark
            if not page.has_more:
                break
        return self.bytes


__all__ = ["MAX_RETAINED_RUN_DIRS", "OUTPUT_LIMIT_BYTES", "ProcessDebugLauncher", "RUN_ROOT_NAME"]
