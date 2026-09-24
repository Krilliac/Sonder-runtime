"""Durable, digest-addressed retention for minimized evaluation failures.

Each :class:`MinimizedFailure` is written as ``<digest>.json`` in one
directory.  Loading re-verifies the trajectory, step, record digests and
divergence consistency, so a corrupted or inconsistent file is refused rather
than replayed.  The digests are integrity checks, not tamper-proofing: anyone
who can write the directory can recompute them.

Writers are serialized by an exclusive ``.lock`` file created with
``O_CREAT | O_EXCL`` and holding a random per-acquisition token, so the
capacity check, the no-overwrite check, and the atomic rename happen as one
critical section.  Ownership is by token:

* release deletes the lock only if it still holds this writer's token;
* a lock older than ``STALE_LOCK_SECONDS`` is broken by first renaming it to a
  unique sidecar (an atomic step only one waiter can win), then checking the
  sidecar still holds the token that was judged stale.  If a new holder had
  replaced the lock in between, its lock is restored with a no-overwrite link
  instead of being discarded.

Temporary files older than ``STALE_TEMPORARY_SECONDS`` are cleaned on write.
The lock is advisory and assumes cooperating writers on one host.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from typing import Iterator

from sonder_runtime.application.evaluation.divergence import (
    DivergenceError,
    MAX_RETAINED_FAILURES,
    MinimizedFailure,
)


MAX_FAILURE_BYTES = 1024 * 1024
STALE_LOCK_SECONDS = 60.0
STALE_TEMPORARY_SECONDS = 300.0
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_NAME = ".lock"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TEMPORARY = re.compile(r"^[0-9a-f]{64}\.json\..+\.tmp$")


class JsonMinimizedFailureStore:
    """File-backed :class:`MinimizedFailureStore` implementation."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_failures: int = MAX_RETAINED_FAILURES,
        max_bytes: int = MAX_FAILURE_BYTES,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if type(max_failures) is not int or not 1 <= max_failures <= MAX_RETAINED_FAILURES:
            raise DivergenceError(f"max_failures must be within 1..{MAX_RETAINED_FAILURES}")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_FAILURE_BYTES:
            raise DivergenceError(f"max_bytes must be within 1..{MAX_FAILURE_BYTES}")
        if (
            isinstance(lock_timeout_seconds, bool) or not isinstance(lock_timeout_seconds, (int, float))
            or not 0 < lock_timeout_seconds <= 60
        ):
            raise DivergenceError("lock_timeout_seconds must be within (0, 60]")
        self.directory = Path(directory)
        self._max_failures = max_failures
        self._max_bytes = max_bytes
        self._lock_timeout = float(lock_timeout_seconds)

    def _path(self, failure_digest: str) -> Path:
        if not isinstance(failure_digest, str) or not _DIGEST.match(failure_digest):
            raise DivergenceError("failure digest must be a lowercase SHA-256 hex digest")
        return self.directory / f"{failure_digest}.json"

    @staticmethod
    def _read_token(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def _break_stale_lock(self, lock: Path, observed_token: str | None) -> bool:
        """Remove ``lock`` only if it still holds ``observed_token``.

        The rename to a unique sidecar is atomic, so of several waiters that
        judged the same lock stale, exactly one moves it.  The mover then checks
        the token; if the file it moved is a newer holder's lock, it is put back
        with a no-overwrite link rather than deleted.
        """
        sidecar = lock.with_name(f"{lock.name}.{secrets.token_hex(8)}.stale")
        try:
            os.rename(lock, sidecar)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        try:
            if self._read_token(sidecar) == observed_token:
                return True
            try:
                os.link(sidecar, lock)
            except OSError:
                pass
            return False
        finally:
            try:
                sidecar.unlink()
            except OSError:
                pass

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock = self.directory / _LOCK_NAME
        token = secrets.token_hex(16)
        deadline = time.monotonic() + self._lock_timeout
        while True:
            try:
                descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                # Only open a lock to read its token once it is stale: on
                # Windows an open reader makes the holder's unlink fail.
                try:
                    stale = time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS
                except FileNotFoundError:
                    continue
                except OSError:
                    stale = False
                if stale and self._break_stale_lock(lock, self._read_token(lock)):
                    continue
                if time.monotonic() >= deadline:
                    raise DivergenceError("minimized failure store lock is held by another writer") from None
                time.sleep(0.02)
                continue
            try:
                os.write(descriptor, token.encode("ascii"))
            finally:
                os.close(descriptor)
            break
        try:
            yield
        finally:
            self._release(lock, token)

    def _release(self, lock: Path, token: str) -> None:
        """Delete the lock only if it still holds ``token``.

        A concurrent reader (a waiter checking a stale lock) can make the
        delete fail transiently on Windows, so it is retried for a bounded time
        rather than silently leaking the lock.
        """
        deadline = time.monotonic() + max(self._lock_timeout, 1.0)
        while True:
            owner = self._read_token(lock)
            if owner is not None and owner != token:
                return
            try:
                lock.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise DivergenceError("could not release the minimized failure store lock") from None
                time.sleep(0.01)

    def _clean_stale_temporaries(self) -> None:
        now = time.time()
        for entry in self.directory.iterdir():
            if entry.is_file() and _TEMPORARY.match(entry.name):
                try:
                    if now - entry.stat().st_mtime > STALE_TEMPORARY_SECONDS:
                        entry.unlink()
                except OSError:
                    pass

    def retain(self, failure: MinimizedFailure) -> str:
        digest = failure.digest
        path = self._path(digest)
        encoded = (
            json.dumps(failure.as_dict(), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
        ).encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise DivergenceError("minimized failure exceeds the retention byte bound")
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._locked():
            self._clean_stale_temporaries()
            if path.exists():
                if self.load(digest).digest != digest:
                    raise DivergenceError("retained failure name does not match its content")
                return digest
            if len(self.digests()) >= self._max_failures:
                raise DivergenceError("minimized failure store is full")
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
