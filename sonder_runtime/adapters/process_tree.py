"""One owner for the lifecycle of a child process and everything it spawns.

Sonder starts child processes in many places.  Each used to hand-roll the same
four steps and each got a different subset right: start the child as the root
of its own tree, wait for it with a bound, tear the whole tree down (polite
first, forced second) and reap what was killed.  This module is that
contract, once:

* **Start** -- POSIX: a new session, so the child is the leader of a process
  group and session that every descendant inherits.  Windows: a new process
  group plus a kill-on-close Job Object that every descendant inherits.
* **Bounded wait** -- ``wait(timeout, cancel)`` returns a typed
  :class:`TreeResult`; it never blocks past its bound and never raises for a
  timeout or a cancellation.
* **Escalating teardown of the whole tree** -- POSIX: ``SIGTERM`` to the group,
  a grace period, then ``SIGKILL`` to the group and to any stray member found
  through ``/proc``.  Windows has no portable polite tree signal, so the
  Job Object is terminated outright (``escalated`` is reported ``True``).
* **Reaping** -- the root is always waited on, so it never lingers as a
  zombie; on POSIX, orphans re-parented to this process (it can be PID 1 in a
  container, or a subreaper) are reaped too.  On Windows the Job Object
  outlives the root, so descendants of an already-exited root are still
  found and removed, which ``taskkill /T`` cannot do.
* **Typed results** -- :class:`TreeResult` says whether the tree was proven
  empty (``complete``), whether force was needed and whether survivors were
  swept after a normal exit.

State and thread affinity: a :class:`ProcessTree` owns one ``Popen`` and, on
Windows, one Job Object handle.  ``terminate`` and ``close`` are serialised by
an internal lock and idempotent; ``wait`` must be called from one thread at a
time.  Every method is safe to call from a timer or cancellation thread.

Known limits (also true of the code this replaces):

* POSIX: a descendant that leaves the session (``setsid``) and the process
  group is not part of the tree.  Linux teardown also reads ``/proc`` to find
  members that only left the process group.
* Windows: ``AssignProcessToJobObject`` happens after ``CreateProcess``, so a
  grandchild created in that gap is not in the job.  If the job cannot be
  created or assigned (already in a job that forbids nesting), teardown falls
  back to ``taskkill /T`` and reports ``complete=False`` because that cannot
  prove the tree is empty.
"""
from __future__ import annotations

import enum
import functools
import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Self

from sonder_runtime.platform.process import CancellationToken

_POLL_SECONDS = 0.02
_WAIT_SLICE_SECONDS = 0.05
DEFAULT_GRACE_SECONDS = 2.0
DEFAULT_KILL_WAIT_SECONDS = 5.0

_CREATE_NEW_PROCESS_GROUP = 0x00000200

# Captured at import so a test that monkeypatches ``subprocess.Popen`` cannot
# make a double look like a real child (and so a fake pid reach ``killpg``).
_REAL_POPEN = subprocess.Popen


def is_real_popen(proc: object) -> bool:
    """True only for a genuine ``subprocess.Popen`` instance."""
    return isinstance(proc, _REAL_POPEN)


class TreeState(enum.Enum):
    """Why :meth:`ProcessTree.wait` returned."""

    EXITED = "exited"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TreeResult:
    """Typed outcome of waiting for, or tearing down, a process tree.

    ``returncode`` is the root's exit status once it has been reaped.
    ``escalated`` is true when force (``SIGKILL`` / ``TerminateJobObject``) was
    needed.  ``swept`` is true when descendants outlived the root's normal exit
    and were removed.  ``complete`` is true only when the root is reaped and
    the tree was observed to be empty.
    """

    state: TreeState
    returncode: int | None
    escalated: bool = False
    swept: bool = False
    complete: bool = True


def group_popen_kwargs(
    creationflags: int = 0, *, os_name: str | None = None,
) -> dict[str, object]:
    """``Popen`` keyword arguments that make the child the root of its own tree."""
    if (os_name or os.name) == "nt":
        return {"creationflags": int(creationflags) | _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# --- POSIX membership -------------------------------------------------------

def _linux_members(pgid: int) -> tuple[int, ...] | None:
    """Live pids whose process group or session is *pgid*; ``None`` off Linux."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    members: list[int] = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as handle:
                raw = handle.read().decode("ascii", "replace")
        except OSError:
            continue
        end = raw.rfind(")")
        if end < 0:
            continue
        fields = raw[end + 1:].split()
        if len(fields) < 4 or fields[0] in ("Z", "X", "x"):
            continue
        try:
            if int(fields[2]) == pgid or int(fields[3]) == pgid:
                members.append(int(name))
        except ValueError:
            continue
    return tuple(sorted(members))


def live_group_members(pgid: int) -> tuple[int, ...]:
    """Live (non-zombie) pids still in process group/session *pgid* (POSIX).

    Linux reads ``/proc``; other POSIX systems can only probe the group with
    signal 0 and report ``(pgid,)`` when it exists.  Windows has no process
    groups in this sense and returns ``()``.
    """
    if os.name == "nt" or pgid <= 0:
        return ()
    scanned = _linux_members(pgid)
    if scanned is not None:
        return scanned
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        return ()
    try:
        killpg(pgid, 0)
    except ProcessLookupError:
        return ()
    except PermissionError:
        return (pgid,)
    except OSError:
        return ()
    return (pgid,)


# --- Windows Job Object -----------------------------------------------------

class _WindowsJob:
    """A kill-on-close Job Object holding one process tree (Windows only)."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _EXTENDED_LIMIT_CLASS = 9
    _ACCOUNTING_CLASS = 1

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle
        self._lock = threading.Lock()

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def _kernel32() -> Any:
        import ctypes
        import ctypes.wintypes as wt

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise AttributeError("ctypes.WinDLL is unavailable")
        kernel32 = win_dll("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wt.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wt.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD, wt.LPVOID,
        ]
        kernel32.QueryInformationJobObject.restype = wt.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wt.BOOL
        kernel32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
        kernel32.TerminateJobObject.restype = wt.BOOL
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.CloseHandle.restype = wt.BOOL
        return kernel32

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def _extended_limit_type() -> Any:
        import ctypes
        import ctypes.wintypes as wt

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wt.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wt.DWORD),
                ("SchedulingClass", wt.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        return ExtendedLimit

    @classmethod
    def create_for(cls, proc: subprocess.Popen[Any]) -> _WindowsJob | None:
        """Create a job holding *proc*; ``None`` when that is not possible."""
        if os.name != "nt":
            return None
        process_handle = getattr(proc, "_handle", None)
        if process_handle is None:
            return None
        try:
            import ctypes

            kernel32 = cls._kernel32()

            ExtendedLimit = cls._extended_limit_type()

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = ExtendedLimit()
            info.BasicLimitInformation.LimitFlags = cls._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job, cls._EXTENDED_LIMIT_CLASS, ctypes.byref(info), ctypes.sizeof(info),
            ):
                kernel32.CloseHandle(job)
                return None
            if not kernel32.AssignProcessToJobObject(job, int(process_handle)):
                kernel32.CloseHandle(job)
                return None
            return cls(int(job))
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def active_processes(self) -> int | None:
        """Processes currently in the job, or ``None`` when it cannot be read."""
        with self._lock:
            if self._handle is None:
                return 0
            try:
                import ctypes
                import ctypes.wintypes as wt

                class Accounting(ctypes.Structure):
                    _fields_ = [
                        ("TotalUserTime", ctypes.c_longlong),
                        ("TotalKernelTime", ctypes.c_longlong),
                        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                        ("TotalPageFaultCount", wt.DWORD),
                        ("TotalProcesses", wt.DWORD),
                        ("ActiveProcesses", wt.DWORD),
                        ("TotalTerminatedProcesses", wt.DWORD),
                    ]

                kernel32 = self._kernel32()
                info = Accounting()
                if not kernel32.QueryInformationJobObject(
                    self._handle, self._ACCOUNTING_CLASS,
                    ctypes.byref(info), ctypes.sizeof(info), None,
                ):
                    return None
                return int(info.ActiveProcesses)
            except (AttributeError, OSError, TypeError, ValueError):
                return None

    def terminate(self) -> bool:
        with self._lock:
            if self._handle is None:
                return True
            try:
                return bool(self._kernel32().TerminateJobObject(self._handle, 1))
            except (AttributeError, OSError, TypeError, ValueError):
                return False

    def release(self) -> None:
        """Close the handle but let whatever is still in the job keep running."""
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            try:
                import ctypes

                info = self._extended_limit_type()()  # all limit flags cleared
                self._kernel32().SetInformationJobObject(
                    handle, self._EXTENDED_LIMIT_CLASS,
                    ctypes.byref(info), ctypes.sizeof(info),
                )
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        self.close()

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            self._kernel32().CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            pass


# --- The tree ---------------------------------------------------------------

class ProcessTree:
    """A child process plus every process it spawns, owned as one unit."""

    def __init__(
        self,
        proc: subprocess.Popen[Any],
        *,
        group_id: int | None,
        job: _WindowsJob | None,
    ) -> None:
        self._proc = proc
        self._group_id = group_id
        self._job = job
        self._lock = threading.RLock()

    # construction

    @classmethod
    def start(
        cls, argv: Sequence[str], **popen_kwargs: Any,
    ) -> ProcessTree:
        """Start *argv* as the root of a new tree.

        ``popen_kwargs`` go to ``subprocess.Popen`` (``shell`` stays false
        unless the caller says otherwise); ``start_new_session`` is forced on
        POSIX and ``CREATE_NEW_PROCESS_GROUP`` is OR-ed into ``creationflags``
        on Windows.  The launcher's ``OSError`` propagates unchanged so callers
        keep their existing missing-binary handling.
        """
        if not argv:
            raise ValueError("argv must be non-empty")
        kwargs: dict[str, Any] = dict(popen_kwargs)
        kwargs.update(group_popen_kwargs(int(kwargs.pop("creationflags", 0) or 0)))
        return cls.owning(_REAL_POPEN(list(argv), **kwargs))

    @classmethod
    def owning(cls, proc: subprocess.Popen[Any]) -> ProcessTree:
        """Wrap a real ``Popen`` the caller just started with ``group_popen_kwargs()``.

        For callers that must keep their own ``Popen`` call (custom pipes,
        injected launchers) but still want this module's teardown.  Call it
        straight after ``Popen`` returns: POSIX records the child as its own
        group leader (true because of ``start_new_session``) so descendants
        of a root that has since exited are still found; Windows creates the
        Job Object while the process handle is still open.  Anything that is
        not a real ``subprocess.Popen`` is refused so a test double's fake pid
        can never be turned into a ``killpg`` target.
        """
        if not is_real_popen(proc):
            raise TypeError("owning() needs a subprocess.Popen")
        if os.name == "nt":
            return cls(proc, group_id=None, job=_WindowsJob.create_for(proc))
        return cls(proc, group_id=proc.pid, job=None)

    @classmethod
    def adopt(cls, proc: subprocess.Popen[Any]) -> ProcessTree:
        """Wrap a ``Popen`` that something else started.

        POSIX: the tree is the child's process group only when the child is
        verifiably the leader of a group that is not ours; otherwise only the
        root itself is managed (never signal a group we cannot prove is the
        child's).  Windows: no Job Object exists, so teardown is the
        ``taskkill /T`` fallback and cannot prove the tree is empty.
        """
        group_id: int | None = None
        getpgid = getattr(os, "getpgid", None)
        getpgrp = getattr(os, "getpgrp", None)
        if os.name != "nt" and proc.poll() is None and getpgid is not None and getpgrp is not None:
            try:
                candidate = int(getpgid(proc.pid))
                if candidate == proc.pid and candidate != int(getpgrp()):
                    group_id = candidate
            except OSError:
                group_id = None
        return cls(proc, group_id=group_id, job=None)

    # introspection

    @property
    def popen(self) -> subprocess.Popen[Any]:
        return self._proc

    @property
    def pid(self) -> int:
        return self._proc.pid

    # waiting

    def wait(
        self,
        timeout: float | None = None,
        *,
        cancel: CancellationToken | None = None,
        sweep_on_exit: bool = True,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        kill_wait_seconds: float = DEFAULT_KILL_WAIT_SECONDS,
    ) -> TreeResult:
        """Wait up to *timeout* seconds; tear the tree down on timeout or cancel.

        A normal root exit returns ``EXITED``.  With ``sweep_on_exit`` any
        descendant still alive afterwards is removed (``swept=True``); pass
        ``False`` to leave a deliberately detached daemon running until
        :meth:`terminate` or :meth:`close`.
        """
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        state = TreeState.EXITED
        while self._proc.poll() is None:
            if cancel is not None and cancel.cancelled:
                state = TreeState.CANCELLED
                break
            slice_seconds = _WAIT_SLICE_SECONDS
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    state = TreeState.TIMED_OUT
                    break
                slice_seconds = min(slice_seconds, remaining)
            try:
                self._proc.wait(timeout=slice_seconds)
            except subprocess.TimeoutExpired:
                continue
        if state is not TreeState.EXITED:
            result = self.terminate(
                grace_seconds=grace_seconds, kill_wait_seconds=kill_wait_seconds,
            )
            return TreeResult(
                state, result.returncode, result.escalated, False, result.complete,
            )
        if not sweep_on_exit or not self.has_survivors():
            return TreeResult(
                TreeState.EXITED, self._proc.returncode, False, False,
                self._tree_empty(),
            )
        result = self.terminate(
            grace_seconds=grace_seconds, kill_wait_seconds=kill_wait_seconds,
        )
        return TreeResult(
            TreeState.EXITED, self._proc.returncode, result.escalated, True,
            result.complete,
        )

    # teardown

    def terminate(
        self,
        *,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        kill_wait_seconds: float = DEFAULT_KILL_WAIT_SECONDS,
    ) -> TreeResult:
        """Remove the whole tree: polite, then forced; always reap the root."""
        if grace_seconds < 0 or kill_wait_seconds < 0:
            raise ValueError("durations must not be negative")
        with self._lock:
            if os.name == "nt":
                escalated, empty = self._terminate_windows(kill_wait_seconds)
            else:
                escalated, empty = self._terminate_posix(
                    grace_seconds, kill_wait_seconds,
                )
            reaped = self._reap_root(kill_wait_seconds)
            return TreeResult(
                TreeState.EXITED, self._proc.returncode, escalated, False,
                bool(empty and reaped),
            )

    def close(self) -> None:
        """Release the Windows Job Object handle, which ends anything left in it."""
        with self._lock:
            if self._job is not None:
                self._job.close()

    def release(self) -> None:
        """Stop owning the tree and leave surviving descendants running.

        For callers that deliberately let a finished command's background
        processes live on.  Windows: drops kill-on-close, then closes the
        handle.  POSIX: nothing to release.
        """
        with self._lock:
            if self._job is not None:
                self._job.release()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # internals: shared

    def _reap_root(self, wait_seconds: float) -> bool:
        try:
            self._proc.wait(timeout=max(wait_seconds, _POLL_SECONDS))
        except subprocess.TimeoutExpired:
            return False
        return True

    def has_survivors(self) -> bool:
        """True while a descendant of the root is still alive."""
        if os.name == "nt":
            if self._job is None:
                return False
            count = self._job.active_processes()
            return bool(count)
        if self._group_id is None:
            return False
        return bool(live_group_members(self._group_id))

    def _tree_empty(self) -> bool:
        if os.name == "nt":
            if self._job is None:
                return False
            return self._job.active_processes() == 0
        if self._group_id is None:
            return self._proc.returncode is not None
        return not live_group_members(self._group_id)

    # internals: POSIX

    def _signal_group(self, sig: int) -> None:
        group = self._group_id
        sigkill = int(getattr(signal, "SIGKILL", signal.SIGTERM))
        if group is None:
            if sig == sigkill and self._proc.poll() is None:
                try:
                    self._proc.kill()
                except OSError:
                    pass
            elif self._proc.poll() is None:
                try:
                    self._proc.send_signal(sig)
                except OSError:
                    pass
            return
        getpgrp = getattr(os, "getpgrp", None)
        killpg = getattr(os, "killpg", None)
        if getpgrp is not None and group == int(getpgrp()):
            return
        if killpg is not None:
            try:
                killpg(group, sig)
            except (ProcessLookupError, PermissionError):
                pass
            except OSError:
                pass
        # Members that left the process group but kept the session.
        for pid in live_group_members(group):
            if pid in (os.getpid(), self._proc.pid):
                continue
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    def _reap_adopted_children(self) -> None:
        """Reap orphans re-parented to us (PID 1 in a container, or subreaper)."""
        group = self._group_id
        wnohang = getattr(os, "WNOHANG", None)
        if group is None or wnohang is None or self._proc.poll() is None:
            return
        while True:
            try:
                pid, _status = os.waitpid(-group, int(wnohang))
            except ChildProcessError:
                return
            except OSError:
                return
            if pid == 0:
                return

    def _group_empty_within(self, seconds: float) -> bool:
        group = self._group_id
        deadline = time.monotonic() + seconds
        while True:
            self._proc.poll()
            if group is None:
                empty = self._proc.returncode is not None
            else:
                empty = not live_group_members(group)
            # Reap after the census: a member killed since the previous pass
            # is a zombie child of ours now and must not outlive this call.
            self._reap_adopted_children()
            if empty:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_SECONDS)

    def _terminate_posix(
        self, grace_seconds: float, kill_wait_seconds: float,
    ) -> tuple[bool, bool]:
        escalated = False
        sigkill = int(getattr(signal, "SIGKILL", signal.SIGTERM))
        self._signal_group(signal.SIGTERM)
        if not self._group_empty_within(grace_seconds):
            escalated = True
            self._signal_group(sigkill)
            empty = self._group_empty_within(kill_wait_seconds)
        else:
            empty = True
        return escalated, empty

    # internals: Windows

    def _terminate_windows(self, kill_wait_seconds: float) -> tuple[bool, bool]:
        job = self._job
        if job is not None and job.terminate():
            deadline = time.monotonic() + kill_wait_seconds
            while True:
                count = job.active_processes()
                if count == 0:
                    return True, True
                if count is None or time.monotonic() >= deadline:
                    return True, False
                time.sleep(_POLL_SECONDS)
        # No job (not created, or terminate failed): best effort only.
        if self._proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            if self._proc.poll() is None:
                try:
                    self._proc.kill()
                except OSError:
                    pass
        return True, False


def terminate_tree(
    proc: subprocess.Popen[Any],
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    kill_wait_seconds: float = DEFAULT_KILL_WAIT_SECONDS,
) -> TreeResult:
    """Tear down a ``Popen`` that was started elsewhere (see :meth:`ProcessTree.adopt`)."""
    return ProcessTree.adopt(proc).terminate(
        grace_seconds=grace_seconds, kill_wait_seconds=kill_wait_seconds,
    )


__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_KILL_WAIT_SECONDS",
    "ProcessTree",
    "TreeResult",
    "TreeState",
    "group_popen_kwargs",
    "is_real_popen",
    "live_group_members",
    "terminate_tree",
]
