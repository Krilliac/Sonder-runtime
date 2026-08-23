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
import os
import time


class LockTimeout(TimeoutError):
    """The lock could not be acquired inside the caller's deadline."""


def _open_lock_descriptor(path):
    if os.path.islink(path):
        raise OSError("refusing to lock through a symbolic link: %s" % path)
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
def exclusive_file_lock(path, *, timeout=30.0, poll_interval=0.05):
    """Hold an exclusive cross-process lock on ``path`` for the with-block.

    ``path`` is the lock sidecar itself (conventionally ``<guarded>.lock``),
    not the file being guarded. Raises :class:`LockTimeout` when the lock is
    still held elsewhere after ``timeout`` seconds; raises ``OSError`` when
    the lock file cannot be created or is a symbolic link.
    """
    timeout = max(0.0, float(timeout))
    poll_interval = max(0.001, float(poll_interval))
    descriptor = _open_lock_descriptor(str(path))
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            if _try_lock(descriptor):
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise LockTimeout(
                    "could not acquire %s within %.1fs" % (path, timeout)
                )
            time.sleep(poll_interval)
        yield
    finally:
        try:
            if acquired:
                _unlock(descriptor)
        finally:
            os.close(descriptor)
