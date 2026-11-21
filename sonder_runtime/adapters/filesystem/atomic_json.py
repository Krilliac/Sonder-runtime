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
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from sonder_runtime.adapters.filesystem.durable_locks import exclusive_file_lock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def _serialise(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def write_json_atomic(path: str | os.PathLike[str], payload: object) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(_serialise(payload), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def write_json_validated(
    path: str | os.PathLike[str],
    payload: object,
    *,
    validate: Callable[[Path], None],
    backup: str | os.PathLike[str] | None,
) -> Path:
    """Write ``payload`` like :func:`write_json_atomic`, after ``validate`` accepts it.

    The bytes go to a same-directory temporary file (flushed and fsynced);
    ``validate(temporary)`` must return without raising, otherwise the
    temporary file is removed and the exception propagates with ``path``
    untouched.  When ``backup`` is given and ``path`` exists, its current
    bytes replace ``backup`` atomically before ``path`` is replaced.  The
    caller serializes writers (for example with :func:`file_lock`).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    backup_path = Path(backup) if backup is not None else None
    backup_temporary = (
        backup_path.with_name(f"{backup_path.name}.tmp-{uuid.uuid4().hex}")
        if backup_path is not None
        else None
    )
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(_serialise(payload))
            stream.flush()
            os.fsync(stream.fileno())
        validate(temporary)
        if backup_path is not None and backup_temporary is not None and path.exists():
            backup_temporary.write_bytes(path.read_bytes())
            os.replace(backup_temporary, backup_path)
        os.replace(temporary, path)
    finally:
        for leftover in (temporary, backup_temporary):
            if leftover is not None and leftover.exists():
                leftover.unlink()
    return path


@contextlib.contextmanager
def file_lock(
    target: str | os.PathLike[str],
    *,
    timeout: float = 10.0,
    suffix: str = ".lock",
) -> Iterator[None]:
    """Serialize access to ``target`` across independent processes."""
    target = Path(target).resolve()
    lock_path = target.with_name(target.name + suffix)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(
        lock_path, timeout=timeout, purpose=f"atomic-json:{target.name}"
    ):
            yield
