"""Cross-process serialization for bounded private compute disk spools."""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import time


@contextmanager
def locked_spool(root: Path):
    """Hold a cross-process OS file lock during scan and publication.

    The OS releases the lock when the holding process dies, so a crashed
    writer never wedges the spool; a later scan counts any bytes it left.
    """
    path = root / ".quota.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError("compute spool quota lock is unsafe")
    with open(path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue  # LK_LOCK gives up after ~10 s; keep waiting
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def counted_directories(root: Path, *, max_directories: int):
    """Yield only real child directories, rejecting unexpected spool objects."""
    count = 0
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name == ".quota.lock":
                continue
            if not entry.is_dir(follow_symlinks=False):
                raise OSError("compute spool contains an unexpected object")
            count += 1
            if count > max_directories:
                raise OSError("compute spool directory limit exceeded")
            yield Path(entry.path)


def regular_file_bytes(directory: Path) -> int:
    total = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                raise OSError("compute spool contains an unexpected file")
            total += entry.stat(follow_symlinks=False).st_size
    return total


def reap_stale_directories(root: Path, *, max_age_seconds: float) -> int:
    """Remove child directories untouched for longer than ``max_age_seconds``.

    Stages and snapshots left by crashed or pre-quota runs would otherwise
    count against the spool limits forever (a long-lived host had 119 such
    stages, enough to refuse every new job). Callers hold ``locked_spool``.
    Only real directories directly under ``root`` are removed; links are
    never followed.
    """
    cutoff = time.time() - max_age_seconds
    removed = 0
    with os.scandir(root) as entries:
        stale = [
            Path(entry.path) for entry in entries
            if entry.is_dir(follow_symlinks=False)
            and entry.stat(follow_symlinks=False).st_mtime < cutoff
        ]
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)
        removed += 0 if path.exists() else 1
    return removed
