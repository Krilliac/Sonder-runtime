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
"""
from __future__ import annotations

import os
import re
import uuid
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
    directory = Path(journal_path).absolute().parent / "worker-effect-hosts"
    with _LEASES_LOCK:
        lease = _LEASES.get(directory)
        if lease is None or lease.pid != os.getpid():
            lease = WorkerEffectHostLease(directory)
            _LEASES[directory] = lease
        return lease


__all__ = ["WorkerEffectHostLease", "host_lease"]
