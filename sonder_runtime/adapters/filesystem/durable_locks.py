"""Cross-process advisory file locks. Stdlib only.

The runtime already carries two ad-hoc lock implementations
(``command_recovery`` and ``adaptive_training``); this module is the shared
seam for new callers so a third copy never needs to exist. It deliberately
stays small:

  - One primitive: an exclusive, advisory, cross-process lock on a sidecar
    file, acquired with a uniform timeout on both Windows (``msvcrt``) and
    POSIX (``fcntl``). Acquisition polls non-blocking attempts so timeout
    semantics do not depend on platform quirks (``msvcrt.locking`` with
    ``LK_LOCK`` has its own hidden 10-second retry loop; ``fcntl.flock``
    blocks forever).
  - The lock file is created if absent and never deleted: unlinking a lock
    file another process may have already opened is a classic race that
    silently splits the lock into two.
  - Symbolic-link lock paths are refused (``O_NOFOLLOW`` where the platform
    supports it, plus an explicit check), matching the command journal's
    stance: lock acquisition must not become a write through an arbitrary
    redirect target.

Thread safety comes from the descriptor model: every acquisition opens its
own descriptor, and both ``fcntl.flock`` and ``msvcrt.locking`` conflict
between descriptors, so two threads of one process serialize exactly like two
processes. There is no reentrancy -- a holder that re-acquires deadlocks
itself on POSIX and fails on Windows -- so keep critical sections small and
never nest.

This is an advisory lock for cooperating Sonder processes, not a security
boundary: a process that ignores the sidecar can still touch the guarded
file.
"""
import contextlib
import json
import logging
import math
import os
import socket
import tempfile
import time
import uuid

_LOG = logging.getLogger(__name__)


class LockTimeout(TimeoutError):
    """The lock could not be acquired inside the caller's deadline."""

    def __init__(self, message, *, holder=None):
        super().__init__(message)
        self.holder = holder


def owner_path(path):
    """Return the diagnostic owner sidecar for a lock path."""
    path = os.fspath(path)
    return path + ".owner.json"


def read_owner(path, *, directory_fd=None):
    """Read a lock owner sidecar, returning ``None`` when it is absent/invalid."""
    sidecar = owner_path(path)
    try:
        if directory_fd is None and os.path.islink(sidecar):
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        name = sidecar if directory_fd is None else os.path.basename(sidecar)
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            raw = stream.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _process_identity(pid):
    try:
        from sonder_runtime.adapters.process_liveness import process_identity

        return process_identity(pid)
    except (ImportError, OSError, RuntimeError):
        return None


def _write_owner(path, purpose, *, directory_fd=None):
    sidecar = owner_path(path)
    if directory_fd is None and os.path.islink(sidecar):
        raise OSError(f"refusing to write lock owner through a symbolic link: {sidecar}")
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "process_identity": _process_identity(os.getpid()),
        "started": time.time(),
        "purpose": purpose or None,
        "owner_token": uuid.uuid4().hex,
    }
    if directory_fd is None:
        parent = os.path.dirname(sidecar) or "."
        fd, temporary = tempfile.mkstemp(prefix=f".{os.path.basename(sidecar)}-", dir=parent)
        destination = sidecar
    else:
        destination = os.path.basename(sidecar)
        temporary = f".{destination}-{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600, dir_fd=directory_fd,
        )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    return payload


def _clear_owner(path, owner, *, directory_fd=None):
    sidecar = owner_path(path)
    current = read_owner(path, directory_fd=directory_fd)
    if current is None or current.get("pid") != owner.get("pid"):
        return
    if current.get("process_identity") != owner.get("process_identity"):
        return
    if current.get("owner_token") != owner.get("owner_token"):
        return
    try:
        os.unlink(sidecar if directory_fd is None else os.path.basename(sidecar), dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    except OSError as error:
        # The record is diagnostic only. A Windows reader holding it open
        # (sharing violation) must not turn a completed critical section into
        # a failure; the next holder replaces it, and its token is ours alone.
        _LOG.warning(
            "lock owner record was not cleared: path=%s error_type=%s",
            sidecar, type(error).__name__,
        )


def _publish_owner(path, purpose, *, directory_fd=None):
    """Best-effort diagnostic owner record for a lock that is already held.

    Returns ``None`` when the record cannot be published. The lock itself is
    authoritative; an unwritable or momentarily shared sidecar must not deny
    or break the guarded operation. Any older record then names a holder that
    is provably gone (this caller holds the lock), so it is removed rather
    than left to misattribute the lock in a waiter's timeout diagnostic.
    """
    try:
        return _write_owner(path, purpose, directory_fd=directory_fd)
    except OSError as error:
        _LOG.warning(
            "lock owner record was not published: path=%s error_type=%s",
            owner_path(path), type(error).__name__,
        )
    sidecar = owner_path(path)
    try:
        # unlink removes a planted link itself and never follows it.
        os.unlink(sidecar if directory_fd is None else os.path.basename(sidecar), dir_fd=directory_fd)
    except OSError:
        pass  # Absent, or still shared: waiters then see a stale record at worst.
    return None


def _open_lock_descriptor(path):
    if os.path.islink(path):
        raise OSError(f"refusing to lock through a symbolic link: {path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600)


def _try_lock(descriptor):
    """One non-blocking acquisition attempt. Returns True on success."""
    if os.name == "nt":
        import msvcrt

        # msvcrt locks a byte range; make sure byte 0 exists to lock.
        if os.fstat(descriptor).st_size < 1:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(descriptor):
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive_descriptor_lock(descriptor, path, *, timeout=30.0, poll_interval=0.05, purpose=None, directory_fd=None):
    """Hold an exclusive cross-process lock on ``path`` for the with-block.

    ``path`` is the lock sidecar itself (conventionally ``<guarded>.lock``),
    not the file being guarded. Raises :class:`LockTimeout` when the lock is
    still held elsewhere after ``timeout`` seconds; raises ``OSError`` when
    the lock file cannot be created or is a symbolic link.
    """
    timeout = float(timeout)
    poll_interval = float(poll_interval)
    if not math.isfinite(timeout) or not math.isfinite(poll_interval):
        raise ValueError("lock timeout and poll interval must be finite")
    timeout = max(0.0, timeout)
    poll_interval = max(0.001, poll_interval)
    path = os.fspath(path)
    acquired = False
    owner = None
    try:
        deadline = time.monotonic() + timeout
        while True:
            if _try_lock(descriptor):
                acquired = True
                owner = _publish_owner(path, purpose, directory_fd=directory_fd)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                holder = read_owner(path, directory_fd=directory_fd)
                detail = f"; holder={json.dumps(holder, sort_keys=True)}" if holder else "; holder=unknown"
                raise LockTimeout(f"could not acquire {path} within {timeout:.1f}s{detail}", holder=holder)
            time.sleep(min(poll_interval, remaining))
        yield
    finally:
        if acquired:
            try:
                if owner is not None:
                    _clear_owner(path, owner, directory_fd=directory_fd)
            finally:
                _unlock(descriptor)


@contextlib.contextmanager
def exclusive_file_lock(path, *, timeout=30.0, poll_interval=0.05, purpose=None):
    path = os.fspath(path)
    descriptor = _open_lock_descriptor(path)
    try:
        with exclusive_descriptor_lock(
            descriptor, path, timeout=timeout, poll_interval=poll_interval, purpose=purpose
        ):
            yield
    finally:
        os.close(descriptor)
