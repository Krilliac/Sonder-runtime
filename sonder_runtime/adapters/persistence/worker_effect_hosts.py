"""Local host-process liveness for the shared worker-effects journal (#515).

Several runtime processes on one node (``serve``, an IDE-launched ``mcp``,
a CLI command) compose the same worker identities ``<family>:<node>`` over
one worker-effects journal.  Worker identity therefore cannot tell a crashed
predecessor's unresolved intent from a live peer's in-flight effect, and the
startup reconciliation pass must not claim owners while a peer is live.

Every process that composes the journal holds an exclusive OS file lock on
its own lease file for its whole lifetime.  The kernel releases the lock when
the process exits, however it exits.  A peer is proven dead only when this
process acquires that peer's lock; missing files are ignored, and a held lock
or any I/O error counts as live (fail closed).  The lease is local evidence
only: it is never a remote lease or a takeover policy.

Some worker runs are shared by every process on the node
(``runtime:process-jobs`` and ``runtime:compute-jobs``): their durable owner
row has one epoch, so whichever process claims it last fences the others.
``acquire_run_lease`` gives such a run its own lock file in the sibling
``worker-effect-runs`` directory.  A process that binds the run to a worker
holds it from the moment it first claims the run until it exits; a peer that
cannot take it must not claim the run.  ``transient_run_lease`` holds the same
lock only for one bounded step (the startup reconciliation pass) and releases
it afterwards unless a worker composition in this process took it meanwhile,
so reconciling a run never keeps a peer from composing that worker.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import BinaryIO

_LEASE_NAME = re.compile(r"host-[0-9]+-[0-9a-f]{32}\.lock\Z")
_MAX_LEASE_SCAN = 256


def _lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class WorkerEffectHostLease:
    """This process's liveness lease beside one worker-effects journal."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.pid = os.getpid()
        self.name = f"host-{self.pid}-{uuid.uuid4().hex}.lock"
        path = self.directory / self.name
        handle = path.open("x+b")
        try:
            handle.write(b"0")
            handle.flush()
            _lock(handle)
        except BaseException:
            handle.close()
            path.unlink(missing_ok=True)
            raise
        self._handle = handle

    def live_peers(self) -> int:
        """Count other host processes proven or presumed live; reap dead leases.

        Raises ``OSError`` when the lease directory cannot be read; callers
        treat that as "a peer may be live".
        """
        live = 0
        for index, entry in enumerate(sorted(self.directory.iterdir())):
            if index >= _MAX_LEASE_SCAN:
                # Too many leases to prove: presume a live peer.
                return live + 1
            if entry.name == self.name or not _LEASE_NAME.fullmatch(entry.name):
                continue
            try:
                handle = entry.open("r+b")
            except FileNotFoundError:
                continue
            except OSError:
                live += 1
                continue
            try:
                try:
                    _lock(handle)
                except OSError:
                    live += 1
                    continue
                # The owner is gone: the kernel released its lock.  Remove
                # the stale lease while still holding it.
                try:
                    entry.unlink()
                except OSError:
                    pass
            finally:
                handle.close()
        return live


_LEASES: dict[Path, WorkerEffectHostLease] = {}
_LEASES_LOCK = Lock()


def host_lease(journal_path: str | os.PathLike[str]) -> WorkerEffectHostLease:
    """Return this process's lease for the journal, acquiring it once.

    The lease is process-wide: every application composed in this process
    shares it, and it is held until the process exits.  A forked child
    acquires its own lease.
    """
    directory = _lease_directory(journal_path)
    with _LEASES_LOCK:
        lease = _LEASES.get(directory)
        if lease is None or lease.pid != os.getpid():
            lease = WorkerEffectHostLease(directory)
            _LEASES[directory] = lease
        return lease


def _lease_directory(journal_path: str | os.PathLike[str]) -> Path:
    return Path(journal_path).absolute().parent / "worker-effect-hosts"


@dataclass(slots=True)
class _RunLease:
    pid: int
    handle: BinaryIO
    # True once a worker composition bound the run: held until process exit.
    permanent: bool = False
    # Open ``transient_run_lease`` scopes in this process.
    transient: int = 0


_RUN_LEASES: dict[Path, _RunLease] = {}


def _run_lease_path(
    journal_path: str | os.PathLike[str], run_id: str, worker_id: str,
) -> Path:
    if not all(isinstance(value, str) and value.strip() for value in (run_id, worker_id)):
        raise ValueError("run lease identity is required")
    digest = hashlib.sha256(f"{run_id}\0{worker_id}".encode("utf-8")).hexdigest()
    directory = Path(journal_path).absolute().parent / "worker-effect-runs"
    return directory / f"run-{digest[:32]}.lock"


def _held_run_lease(path: Path) -> _RunLease | None:
    """This process's lease on ``path``, locking the file when not yet held.

    Returns ``None`` when another live process holds the lock.  Callers hold
    ``_LEASES_LOCK``.
    """
    lease = _RUN_LEASES.get(path)
    if lease is not None and lease.pid == os.getpid():
        return lease
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("x+b")
    except FileExistsError:
        handle = path.open("r+b")
    else:
        try:
            handle.write(b"0")
            handle.flush()
        except BaseException:
            handle.close()
            raise
    try:
        _lock(handle)
    except OSError:
        # A live process holds the run.  The file stays: it names a fixed
        # run, not a process, so there is nothing to reap.
        handle.close()
        return None
    except BaseException:
        handle.close()
        raise
    lease = _RunLease(os.getpid(), handle)
    _RUN_LEASES[path] = lease
    return lease


def acquire_run_lease(
    journal_path: str | os.PathLike[str], run_id: str, worker_id: str,
) -> bool:
    """Hold this process's exclusive lock on one node-shared worker run.

    Returns ``True`` when this process holds the lock (acquired now or
    earlier; it is kept until the process exits) and ``False`` when another
    live process holds it.  Every composition in one process shares the
    lock, and a forked child must acquire its own.  ``OSError`` from creating
    or opening the lock file propagates; callers refuse the claim.
    """
    path = _run_lease_path(journal_path, run_id, worker_id)
    with _LEASES_LOCK:
        lease = _held_run_lease(path)
        if lease is None:
            return False
        lease.permanent = True
        return True


@contextmanager
def transient_run_lease(
    journal_path: str | os.PathLike[str], run_id: str, worker_id: str,
) -> Iterator[bool]:
    """Hold the run's lock for one bounded step, yielding whether it is held.

    Yields ``False`` (and holds nothing) while another live process holds the
    lock.  On exit the lock is released unless ``acquire_run_lease`` took it
    in this process before or during the step, or another transient scope is
    still open; a later worker composition then takes it again.  ``OSError``
    from creating or opening the lock file propagates on entry.
    """
    path = _run_lease_path(journal_path, run_id, worker_id)
    with _LEASES_LOCK:
        lease = _held_run_lease(path)
        if lease is not None:
            lease.transient += 1
    if lease is None:
        yield False
        return
    try:
        yield True
    finally:
        with _LEASES_LOCK:
            lease.transient -= 1
            if (
                lease.transient == 0 and not lease.permanent
                and _RUN_LEASES.get(path) is lease
            ):
                del _RUN_LEASES[path]
                try:
                    _unlock(lease.handle)
                finally:
                    # Closing the only descriptor also drops the lock.
                    lease.handle.close()


__all__ = [
    "WorkerEffectHostLease", "acquire_run_lease", "host_lease", "transient_run_lease",
]
