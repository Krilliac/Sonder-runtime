"""Atomic JSON writes and cross-process file locks (SPEC-3 Phase 2).

Extracted from the root ``runtime_policy.py``; the root module delegates
here. Behavior is unchanged: write-to-temp + ``os.replace`` for atomic
visibility, and an advisory lock (``msvcrt`` on Windows, ``fcntl`` on
POSIX) serializing read/check/replace across independent processes.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from pathlib import Path


def write_json_atomic(path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("%s.tmp-%s" % (path.name, uuid.uuid4().hex))
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


@contextlib.contextmanager
def file_lock(target: Path, *, timeout: float = 10.0, suffix: str = ".lock"):
    """Serialize access to ``target`` across independent processes."""
    target = Path(target).resolve()
    lock_path = target.with_name(target.name + suffix)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        acquired = False
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":  # pragma: no cover - windows only
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "timed out waiting for %s lock" % target.name
                    ) from exc
                time.sleep(0.02)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":  # pragma: no cover - windows only
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
