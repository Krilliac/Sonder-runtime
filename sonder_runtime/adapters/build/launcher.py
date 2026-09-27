"""Launch a planned build action as a durable process job and reap it.

Mirrors the structured-test launcher: the job goes through the runtime's
``ProcessJobProvider`` with a replacement environment
(``inherit_environment=False``), a hard deadline, a descendant and memory
bound and process-tree termination on cancel or deadline. One reaper thread
per job (from ``platform.runtime_threads``) is the only caller of
``provider.wait``; it releases the build-dir lease when the job ends, after
sweeping the job's session: build tools move their commands into their own
process groups (ninja does), so a process-group kill alone can leave a custom
command running -- the sweep kills whatever is left in the session.

Output (F8): the job registry keeps only a bounded head of a job's output,
far too little for an engine-scale log whose first error may sit in the
middle. The build therefore runs under a fixed tee: the runtime's own
interpreter (``-I -S``, standard library only) starts the planned argv with
stdout and stderr merged, writes every byte to ``<log_dir>/output.log``
(owner-only, created exclusively, bounded at 512 MiB) and forwards a bounded
UTF-8 prefix to the registry. The tee is in the job's process group (POSIX)
or job object (Windows), so a cancel or a deadline takes it with the build.

Before a CMake configure the launcher writes the File API query
(``build_dir/.cmake/api/v1/query/client-sonder/query.json``, fixed bytes) --
the only write outside private state. It refuses a ``build_dir`` that
exists but is neither empty nor a CMake tree, which protects a source tree
mistyped as the build directory.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ...application.build.ports import (
    BUILD_JOB_KIND,
    BUILD_TREE_REJECTED,
    BuildJobPlan,
    build_error,
)
from ...application.context import OperationContext
from ...application.execution.process_jobs import ProcessJobRequest
from ...application.ports.jobs import JobIdentity, JobRecord, JobStatus
from ...platform import runtime_threads
from ...platform.private_files import ensure_private_dir

logger = logging.getLogger(__name__)

RUN_ROOT_NAME = "build-runs"
LOG_FILE_NAME = "output.log"
MAX_RETAINED_RUN_DIRS = 32
MAX_LOG_BYTES = 512 * 1024 * 1024
FORWARD_BYTES = 256 * 1024
QUERY_RELATIVE = (".cmake", "api", "v1", "query", "client-sonder", "query.json")
MAX_QUERY_BYTES = 4096
_REAP_SLICE_SECONDS = 0.5
_REAP_GRACE_SECONDS = 120.0
_MAX_REAP_FAILURES = 50
_CANCEL_PROOF_SECONDS = 15.0
_MAX_REMEMBERED_RUNS = 256
_MAX_BUILD_DIR_PROBE = 64

# The tee. Fixed source, run with ``-I -S`` (no site, no user env, no cwd on
# sys.path). argv: <log> <max log bytes> <max forwarded bytes> -- <argv...>
TEE_SOURCE = r'''
import codecs, os, signal, subprocess, sys
log_path, cap, forward_cap = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
if sys.argv[4] != "--":
    sys.exit(125)
command = sys.argv[5:]
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
log_fd = os.open(log_path, flags, 0o600)
out = sys.stdout.buffer
decoder = codecs.getincrementaldecoder("utf-8")("replace")
state = {"written": 0, "forwarded": 0, "log_full": False, "fwd_full": False, "pipe": True}
def write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        view = view[count:]
def emit(text):
    if not state["pipe"]:
        return
    try:
        out.write(text.encode("utf-8", "replace"))
        out.flush()
    except (BrokenPipeError, OSError, ValueError):
        state["pipe"] = False
def record(chunk):
    if state["written"] < cap:
        part = chunk[: cap - state["written"]]
        write_all(log_fd, part)
        state["written"] += len(part)
    elif not state["log_full"]:
        state["log_full"] = True
        write_all(log_fd, b"\n[sonder: build log capped at %d bytes]\n" % cap)
    if state["forwarded"] < forward_cap:
        part = chunk[: forward_cap - state["forwarded"]]
        state["forwarded"] += len(part)
        emit(decoder.decode(part))
    elif not state["fwd_full"]:
        state["fwd_full"] = True
        emit(decoder.decode(b"", final=True) + "\n[sonder: full output is in the private build log]\n")
try:
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, close_fds=True)
except OSError as exc:
    message = "sonder: could not start %s: %s\n" % (os.path.basename(command[0]) if command else "?", exc.strerror)
    write_all(log_fd, message.encode("utf-8", "replace"))
    os.close(log_fd)
    emit(message)
    sys.exit(127)
if os.name != "nt":
    def forward(signum, frame):
        try:
            child.send_signal(signum)
        except OSError:
            pass
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, forward)
reader = child.stdout
while True:
    try:
        chunk = reader.read1(65536)
    except InterruptedError:
        continue
    if not chunk:
        break
    record(chunk)
code = child.wait()
if state["forwarded"] < forward_cap:
    emit(decoder.decode(b"", final=True))
os.close(log_fd)
sys.exit(code if code >= 0 else 128 - code)
'''
_TEE_BOOTSTRAP = "import base64;exec(base64.b64decode(%r).decode())" % (
    base64.b64encode(TEE_SOURCE.encode("utf-8")).decode("ascii"),
)


def tee_argv(python: str, log_file: str, command: tuple[str, ...], *,
             max_log_bytes: int = MAX_LOG_BYTES, forward_bytes: int = FORWARD_BYTES) -> tuple[str, ...]:
    """The fixed tee wrapper around ``command``."""
    return (python, "-I", "-S", "-c", _TEE_BOOTSTRAP, log_file, str(int(max_log_bytes)),
            str(int(forward_bytes)), "--", *command)


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
    __slots__ = ("principal_id", "done", "exit_code", "log_dir", "on_exit", "session_id", "swept")

    def __init__(self, principal_id: str, log_dir: str,
                 on_exit: Callable[[str], None] | None) -> None:
        self.principal_id = principal_id
        self.done = threading.Event()
        self.exit_code: int | None = None
        self.log_dir = log_dir
        self.on_exit = on_exit
        self.session_id: int | None = None
        self.swept: bool | None = None


def _proc_session_members(session_id: int, proc_root: str = "/proc") -> list[int]:
    """Pids whose session is ``session_id`` (Linux ``/proc``)."""
    members: list[int] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return members
    own = os.getpid()
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == own:
            continue
        try:
            with open(os.path.join(proc_root, name, "stat"), "rb") as handle:
                raw = handle.read(4096)
        except OSError:
            continue
        tail = raw.rsplit(b")", 1)[-1].split()
        # fields after the command: state ppid pgrp session ...
        if len(tail) >= 4 and tail[3].isdigit() and int(tail[3]) == session_id and tail[0] != b"Z":
            members.append(pid)
    return members


def sweep_session(session_id: int | None, *, proc_root: str = "/proc", attempts: int = 40) -> bool | None:
    """SIGKILL every process left in a build's session; True once none remain.

    Build tools put their commands in their own process groups (ninja does,
    for Ctrl-C handling), so killing the job's process group can leave a
    custom command running. They all stay in the job's session, whose id is
    the tee's pid (the job starts with a new session). None where there is no
    ``/proc`` to prove it.
    """
    if session_id is None or session_id <= 1 or os.name == "nt" or not os.path.isdir(proc_root):
        return None
    import signal as signal_module

    pause = threading.Event()
    for _ in range(max(1, attempts)):
        members = _proc_session_members(session_id, proc_root)
        if not members:
            return True
        for pid in members:
            try:
                os.kill(pid, signal_module.SIGKILL)
            except OSError:
                pass
        pause.wait(0.05)
    return not _proc_session_members(session_id, proc_root)


def write_file_api_query(build_dir: str, payload: bytes) -> str:
    """Write the fixed File API query under ``build_dir``; no-follow, bounded.

    Refuses a ``build_dir`` that exists but is neither empty nor a CMake build
    tree (``CMakeCache.txt``), and any symlink or junction on the way down.
    """
    if not isinstance(payload, (bytes, bytearray)) or not payload or len(payload) > MAX_QUERY_BYTES:
        raise build_error(BUILD_TREE_REJECTED, "the File API query is not a fixed document")
    root = Path(build_dir)
    if _is_reparse(root):
        raise build_error(BUILD_TREE_REJECTED, "the build directory is a symlink or junction")
    if root.exists():
        if not root.is_dir():
            raise build_error(BUILD_TREE_REJECTED, "the build directory is not a directory")
        try:
            with os.scandir(root) as entries:
                names = [entry.name for index, entry in enumerate(entries) if index < _MAX_BUILD_DIR_PROBE]
        except OSError:
            raise build_error(BUILD_TREE_REJECTED, "the build directory cannot be listed") from None
        cache = root / "CMakeCache.txt"
        if names and not (os.path.lexists(cache) and not _is_reparse(cache) and cache.is_file()):
            # Only our own earlier query may exist in an otherwise empty tree.
            if set(names) != {".cmake"}:
                raise build_error(BUILD_TREE_REJECTED,
                                  "the build directory holds files but is not a CMake build tree")
    else:
        root.mkdir(parents=True, exist_ok=True)
    current = root
    for part in QUERY_RELATIVE[:-1]:
        current = current / part
        if os.path.lexists(current):
            if _is_reparse(current) or not current.is_dir():
                raise build_error(BUILD_TREE_REJECTED, "the File API query path is not a plain directory")
        else:
            current.mkdir()
    target = current / QUERY_RELATIVE[-1]
    if os.path.lexists(target) and (_is_reparse(target) or not target.is_file()):
        raise build_error(BUILD_TREE_REJECTED, "the File API query path is not a plain file")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(target, flags, 0o644)
    try:
        view = memoryview(bytes(payload))
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
    return str(target)


class ProcessBuildLauncher:
    """``BuildLauncher`` over the durable ``ProcessJobProvider``."""

    def __init__(self, provider_getter: Callable[[], Any], registry_getter: Callable[[], Any], *,
                 executable_guard: Callable[[str], str], run_root: str,
                 python_executable: str | None = None,
                 clock: Callable[[], float] = time.time,
                 max_retained: int = MAX_RETAINED_RUN_DIRS,
                 max_log_bytes: int = MAX_LOG_BYTES,
                 forward_bytes: int = FORWARD_BYTES) -> None:
        self._provider_getter = provider_getter
        self._registry_getter = registry_getter
        self._guard = executable_guard
        self._run_root = Path(run_root)
        self._python = python_executable or sys.executable
        self._clock = clock
        self._max_retained = max(2, int(max_retained))
        self._max_log_bytes = int(max_log_bytes)
        self._forward_bytes = int(forward_bytes)
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    @property
    def run_root(self) -> Path:
        return self._run_root

    # -- launch ----------------------------------------------------------------

    def _check_executables(self, plan: BuildJobPlan) -> None:
        if not plan.checked_executables:
            raise PermissionError("a build plan must name its host executables")
        for path in plan.checked_executables:
            self._guard(path)  # raises PermissionError; re-checked at launch (TOCTOU)
        python = self._python
        if not python or not os.path.isabs(python) or not os.path.isfile(python):
            raise PermissionError("the runtime interpreter is unavailable for the build log tee")

    def _prepare_log_dir(self, plan: BuildJobPlan, job_id: str) -> None:
        log_dir = Path(plan.log_dir)
        if log_dir.parent != self._run_root or log_dir.name != job_id:
            raise PermissionError("build log directory is outside the build-run root")
        if Path(plan.log_file) != log_dir / LOG_FILE_NAME:
            raise PermissionError("build log file is not the run's private log")
        for extra in (*plan.extra_logs, *( (plan.binlog,) if plan.binlog else ())):
            if Path(extra).parent != log_dir:
                raise PermissionError("build artifacts must stay in the run's private directory")
        ensure_private_dir(self._run_root)
        if _is_reparse(self._run_root):
            raise PermissionError("build-run root is a symlink")
        self._prune()
        os.mkdir(log_dir, 0o700)  # exclusive: FileExistsError on reuse
        if os.name != "nt":
            os.chmod(log_dir, 0o700)

    def _prune(self) -> None:
        try:
            with os.scandir(self._run_root) as iterator:
                entries = [entry for index, entry in enumerate(iterator) if index < 4096]
        except OSError:
            return
        with self._lock:
            active = {Path(run.log_dir).name for run in self._runs.values() if not run.done.is_set()}
        candidates = []
        for entry in entries:
            if entry.name in active or not entry.is_dir(follow_symlinks=False):
                continue
            record = self.poll(entry.name)
            if record is not None and not record.is_terminal:
                continue  # running elsewhere or interrupted: evidence is kept
            try:
                candidates.append((entry.stat(follow_symlinks=False).st_mtime, entry.path))
            except OSError:
                continue
        excess = len(candidates) - (self._max_retained - 1)
        for _, path in sorted(candidates)[: max(0, excess)]:
            try:
                shutil.rmtree(path)
            except OSError:
                logger.warning("could not prune an old build run directory", exc_info=True)

    def _pre_writes(self, plan: BuildJobPlan) -> None:
        for path, payload in plan.pre_writes:
            expected = Path(plan.build_dir).joinpath(*QUERY_RELATIVE)
            if Path(path) != expected:
                raise PermissionError("the only pre-launch write is the File API query")
            write_file_api_query(plan.build_dir, payload)

    def start(self, plan: BuildJobPlan, context: OperationContext, job_id: str, *,
              parent_job_id: str = "", on_exit: Callable[[str], None] | None = None) -> None:
        self._check_executables(plan)
        self._prepare_log_dir(plan, job_id)
        try:
            self._pre_writes(plan)
        except BaseException:
            shutil.rmtree(plan.log_dir, ignore_errors=True)
            raise
        metadata = (
            ("principal_id", context.principal_id),
            ("action", plan.action),
            ("system", plan.system),
            ("template_id", plan.template_id),
            ("command_digest", plan.command_digest),
            ("model_digest", plan.model_digest),
            ("cwd_label", plan.cwd_label),
            ("project_label", plan.project_label),
            ("project_root", plan.project_root),
            ("build_dir", plan.build_dir),
            ("log_dir", plan.log_dir),
            ("log_file", plan.log_file),
            ("extra_logs_json", json.dumps(list(plan.extra_logs))[:4096]),
            ("binlog", plan.binlog),
            ("target", plan.target),
            ("config", plan.config),
            ("platform", plan.platform),
            ("file_label", plan.file_label),
            ("trace_family", plan.trace_family),
            ("trace_forced_json", json.dumps(list(plan.trace_forced)[:16])[:4096]),
            ("world", plan.world),
            ("network", plan.network),
            ("isolation_truth", plan.isolation_truth),
            ("parent_job_id", parent_job_id),
            ("display_argv_json", json.dumps(list(plan.display_argv), ensure_ascii=False)[:4096]),
            ("notes_json", json.dumps(list(plan.notes), ensure_ascii=False)[:4096]),
            ("started_at", "%.3f" % self._clock()),
        )
        argv = tee_argv(self._python, plan.log_file, tuple(plan.argv),
                        max_log_bytes=self._max_log_bytes, forward_bytes=self._forward_bytes)
        request = ProcessJobRequest(
            JobIdentity(job_id, BUILD_JOB_KIND, context.correlation_id, job_id,
                        parent_job_id=parent_job_id or None,
                        parent_session_id=getattr(context, "session_id", None)),
            argv,
            cwd=Path(plan.cwd),
            environment=tuple(plan.environment),
            inherit_environment=False,
            require_job_scope=False,
            max_descendants=plan.max_descendants,
            deadline_seconds=plan.timeout_seconds,
            memory_limit_bytes=plan.memory_limit_bytes,
            metadata=metadata,
        )
        run = _Run(context.principal_id, plan.log_dir, on_exit)
        with self._lock:
            finished = [key for key, item in self._runs.items() if item.done.is_set()]
            for key in finished[: max(0, len(finished) - _MAX_REMEMBERED_RUNS)]:
                self._runs.pop(key, None)
            self._runs[job_id] = run
        try:
            started = self._provider_getter().start(request)
            run.session_id = getattr(started, "process_group_id", None) or getattr(started, "process_id", None)
        except BaseException:
            with self._lock:
                self._runs.pop(job_id, None)
            run.done.set()
            shutil.rmtree(plan.log_dir, ignore_errors=True)
            raise
        reaper = runtime_threads.Thread(
            target=self._reap, args=(job_id, run, plan.timeout_seconds),
            name="sonder-build-job-reaper", daemon=True,
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
                    logger.warning("build job reaper wait failed", exc_info=True)
                else:
                    if not waited.timed_out:
                        run.exit_code = waited.exit_code
                        if waited.record.is_terminal or self._terminal(job_id):
                            return
                        failures += 1
                if failures:
                    if failures >= _MAX_REAP_FAILURES:
                        logger.error("build job reaper gave up; the deadline controller owns cleanup")
                        return
                    run.done.wait(0.2)
        finally:
            # Commands a build tool moved to their own process group survive a
            # group kill; nothing of a finished job may outlive it.
            try:
                run.swept = sweep_session(run.session_id)
            except Exception:
                logger.warning("build job session sweep failed", exc_info=True)
                run.swept = False
            run.done.set()
            if run.on_exit is not None:
                try:
                    run.on_exit(job_id)
                except Exception:
                    logger.warning("build job exit hook failed", exc_info=True)

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
        """Cancel the job's process tree; True once cleanup is proven."""
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
            swept = sweep_session(run.session_id)
            if swept is False:
                cleaned = False
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

    def is_active(self, job_id: str) -> bool:
        """Whether a job is known here and not finished (lease staleness check)."""
        with self._lock:
            run = self._runs.get(job_id)
        if run is not None:
            return not run.done.is_set()
        record = self.poll(job_id)
        return record is not None and record.status not in (
            JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


__all__ = [
    "FORWARD_BYTES", "LOG_FILE_NAME", "MAX_LOG_BYTES", "MAX_RETAINED_RUN_DIRS",
    "ProcessBuildLauncher", "QUERY_RELATIVE", "RUN_ROOT_NAME", "TEE_SOURCE", "sweep_session", "tee_argv",
    "write_file_api_query",
]
