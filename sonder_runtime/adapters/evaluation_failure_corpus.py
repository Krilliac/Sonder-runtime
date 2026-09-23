"""Durable, digest-addressed retention for minimized evaluation failures.

Each :class:`MinimizedFailure` is written atomically as ``<digest>.json`` in
one directory.  Loading re-verifies the trajectory, step, and record digests,
so a tampered or truncated file is refused rather than replayed.  The store is
bounded by file count and per-file bytes and never overwrites a different
record under an existing digest name.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

from sonder_runtime.application.evaluation.divergence import (
    DivergenceError,
    MAX_RETAINED_FAILURES,
    MinimizedFailure,
)


MAX_FAILURE_BYTES = 1024 * 1024
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class JsonMinimizedFailureStore:
    """File-backed :class:`MinimizedFailureStore` implementation."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_failures: int = MAX_RETAINED_FAILURES,
        max_bytes: int = MAX_FAILURE_BYTES,
    ) -> None:
        if type(max_failures) is not int or not 1 <= max_failures <= MAX_RETAINED_FAILURES:
            raise DivergenceError(f"max_failures must be within 1..{MAX_RETAINED_FAILURES}")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_FAILURE_BYTES:
            raise DivergenceError(f"max_bytes must be within 1..{MAX_FAILURE_BYTES}")
        self.directory = Path(directory)
        self._max_failures = max_failures
        self._max_bytes = max_bytes

    def _path(self, failure_digest: str) -> Path:
        if not isinstance(failure_digest, str) or not _DIGEST.match(failure_digest):
            raise DivergenceError("failure digest must be a lowercase SHA-256 hex digest")
        return self.directory / f"{failure_digest}.json"

    def retain(self, failure: MinimizedFailure) -> str:
        digest = failure.digest
        path = self._path(digest)
        if path.exists():
            if self.load(digest).digest != digest:
                raise DivergenceError("retained failure name does not match its content")
            return digest
        if len(self.digests()) >= self._max_failures:
            raise DivergenceError("minimized failure store is full")
        encoded = (json.dumps(failure.as_dict(), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise DivergenceError("minimized failure exceeds the retention byte bound")
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(self.directory))
        try:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return digest

    def load(self, failure_digest: str) -> MinimizedFailure:
        path = self._path(failure_digest)
        try:
            size = path.stat().st_size
            if not 1 <= size <= self._max_bytes:
                raise DivergenceError("retained failure is empty or exceeds the byte bound")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except DivergenceError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise DivergenceError("retained failure is unreadable") from exc
        failure = MinimizedFailure.from_dict(payload)
        if failure.digest != failure_digest:
            raise DivergenceError("retained failure name does not match its content")
        return failure

    def digests(self) -> tuple[str, ...]:
        if not self.directory.is_dir():
            return ()
        names = sorted(
            entry.name[:-5] for entry in self.directory.iterdir()
            if entry.is_file() and entry.name.endswith(".json") and _DIGEST.match(entry.name[:-5])
        )
        return tuple(names)


__all__ = ["JsonMinimizedFailureStore", "MAX_FAILURE_BYTES"]
