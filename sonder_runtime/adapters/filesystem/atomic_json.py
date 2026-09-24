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

from sonder_runtime.adapters.filesystem.durable_locks import exclusive_file_lock


def write_json_atomic(path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
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
    with exclusive_file_lock(
        lock_path, timeout=timeout, purpose=f"atomic-json:{target.name}"
    ):
            yield
