"""Bounded JSON-lines host for an extension running in a child process.

The child protocol is intentionally small:

* it writes ``{"type": "ready"}`` during startup;
* the host writes ``{"id": ..., "method": ..., "params": ...}`` per call;
* the child writes one JSON object with the same ``id`` per call.

The host owns the child lifetime.  A timeout, malformed response, oversized
line, or unexpected exit tears the process down before a bounded recovery
attempt.  This module is standard-library only and does not load extension
code in-process.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from typing import Any, Mapping, Sequence


class ExtensionHostError(RuntimeError):
    """Base error for a failed extension-host lifecycle or protocol exchange."""


class ExtensionHostTimeout(ExtensionHostError, TimeoutError):
    """The child exceeded the bounded startup or call window."""


class ExtensionHostCrashed(ExtensionHostError):
    """The child exited unexpectedly or exceeded its recovery budget."""


class ExtensionHostProtocolError(ExtensionHostError):
    """The child violated the JSON-lines host protocol."""


class ExtensionHostOutputLimit(ExtensionHostProtocolError):
    """A single child output line exceeded the configured byte bound."""


@dataclass(frozen=True, slots=True)
class ExtensionHostLimits:
    """Finite process and protocol limits.

    ``max_restarts`` counts recovery launches after the initial launch.
    ``max_crashes`` counts unexpected non-zero exits.  A timeout or protocol
    failure may consume restart budget, but only a non-zero exit consumes
    crash budget.
    """

    startup_timeout_seconds: float = 2.0
    call_timeout_seconds: float = 5.0
    max_output_bytes: int = 64 * 1024
    max_restarts: int = 2
    max_crashes: int = 3

    def __post_init__(self) -> None:
        for value, name in (
            (self.startup_timeout_seconds, "startup_timeout_seconds"),
            (self.call_timeout_seconds, "call_timeout_seconds"),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        for value, name in (
            (self.max_output_bytes, "max_output_bytes"),
            (self.max_restarts, "max_restarts"),
            (self.max_crashes, "max_crashes"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.max_output_bytes < 2:
            raise ValueError("max_output_bytes must allow a JSON line and newline")


@dataclass(frozen=True, slots=True)
class ExtensionHostStats:
    """Safe lifecycle counters; command lines and environment are excluded."""

    launches: int = 0
    restarts: int = 0
    crashes: int = 0
    calls: int = 0


class ExtensionHost:
    """Run an extension behind a bounded, restartable JSON-lines boundary."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        limits: ExtensionHostLimits | None = None,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        popen=subprocess.Popen,
    ) -> None:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("extension host argv must contain non-empty strings")
        if env is not None and any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in env.items()
        ):
            raise ValueError("extension host environment must contain string pairs")
        self._argv = tuple(argv)
        self._limits = limits or ExtensionHostLimits()
        self._cwd = Path(cwd) if cwd is not None else None
        self._env = dict(env) if env is not None else None
        self._popen = popen
        self._process: subprocess.Popen[bytes] | None = None
        self._launches = 0
        self._restarts = 0
        self._crashes = 0
        self._calls = 0
        self._next_id = 1
        self._lock = threading.RLock()

    @property
    def stats(self) -> ExtensionHostStats:
        return ExtensionHostStats(self._launches, self._restarts, self._crashes, self._calls)

    def start(self) -> None:
        """Launch the child and require its bounded startup handshake."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return
            if self._process is not None:
                self._discard_process(self._process)
                self._process = None
            self._launch_ready(recovery=False)

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and return its matching JSON object.

        Failed calls are not replayed because an extension method may have
        side effects.  The host is recovered for an explicit subsequent call
        when the restart/crash budget permits it.
        """
        if not isinstance(method, str) or not method:
            raise ValueError("extension method must be a non-empty string")
        if params is not None and not isinstance(params, Mapping):
            raise TypeError("extension params must be a mapping")
        with self._lock:
            self.start()
            process = self._require_process()
            request_id = self._next_id
            self._next_id += 1
            request = {"id": request_id, "method": method, "params": dict(params or {})}
            try:
                self._write_json(process, request)
                result = self._read_json(process, self._limits.call_timeout_seconds)
            except ExtensionHostError:
                self._recover_after_failure(process)
                raise
            self._calls += 1
            if result.get("id") != request_id:
                self._recover_after_failure(process)
                raise ExtensionHostProtocolError("extension response id did not match request")
            return result

    def close(self) -> None:
        """Terminate and reap the child, if running."""
        with self._lock:
            process = self._process
            self._process = None
            if process is not None:
                self._discard_process(process)

    def _launch_ready(self, *, recovery: bool) -> None:
        if recovery:
            if self._restarts >= self._limits.max_restarts:
                raise ExtensionHostCrashed("extension restart budget exhausted")
            self._restarts += 1
        process = self._popen(
            list(self._argv),
            cwd=self._cwd,
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._process = process
        self._launches += 1
        try:
            ready = self._read_json(process, self._limits.startup_timeout_seconds)
            if ready != {"type": "ready"}:
                raise ExtensionHostProtocolError("extension startup response must be {type: ready}")
        except ExtensionHostError:
            self._discard_process(process)
            self._process = None
            if process.returncode not in (None, 0):
                self._crashes += 1
            raise

    def _recover_after_failure(self, process: subprocess.Popen[bytes]) -> None:
        self._discard_process(process)
        crashed = process.returncode not in (None, 0)
        if self._process is process:
            self._process = None
        if crashed:
            self._crashes += 1
            if self._crashes > self._limits.max_crashes:
                raise ExtensionHostCrashed("extension crash budget exhausted")
        if self._restarts >= self._limits.max_restarts:
            return
        try:
            self._launch_ready(recovery=True)
        except ExtensionHostError:
            # Preserve the original bounded failure; the next explicit call
            # can observe the exhausted/remaining recovery state.
            self._process = None

    def _require_process(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is None or process.poll() is not None:
            raise ExtensionHostCrashed("extension process is not running")
        if process.stdin is None or process.stdout is None:
            raise ExtensionHostError("extension process pipes are unavailable")
        return process

    def _write_json(self, process: subprocess.Popen[bytes], value: Mapping[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > self._limits.max_output_bytes:
            raise ExtensionHostOutputLimit("extension request exceeds output byte bound")
        try:
            process.stdin.write(payload)  # type: ignore[union-attr]
            process.stdin.flush()  # type: ignore[union-attr]
        except (BrokenPipeError, OSError) as exc:
            raise ExtensionHostCrashed("extension process pipe closed") from exc

    def _read_json(self, process: subprocess.Popen[bytes], timeout: float) -> dict[str, Any]:
        if process.stdout is None:
            raise ExtensionHostError("extension stdout pipe is unavailable")
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

        def reader() -> None:
            try:
                line = bytearray()
                while True:
                    read_chunk = getattr(process.stdout, "read1", process.stdout.read)
                    chunk = read_chunk(4096)
                    if not chunk:
                        raise ExtensionHostCrashed("extension closed stdout before a response")
                    line.extend(chunk)
                    if len(line) > self._limits.max_output_bytes:
                        raise ExtensionHostOutputLimit("extension output exceeds byte bound")
                    if b"\n" in chunk:
                        line = line[: line.index(b"\n")].rstrip(b"\r")
                        try:
                            value = json.loads(bytes(line).decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                            raise ExtensionHostProtocolError("extension response was not valid JSON") from exc
                        result_queue.put(("value", value))
                        return
            except ExtensionHostError as exc:
                result_queue.put(("error", exc))
            except OSError as exc:
                result_queue.put(("error", ExtensionHostCrashed("extension stdout read failed")))

        thread = threading.Thread(target=reader, name="sonder-extension-reader", daemon=True)
        thread.start()
        try:
            kind, value = result_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise ExtensionHostTimeout("extension exceeded its bounded timeout") from exc
        if kind == "error":
            raise value  # type: ignore[misc]
        if not isinstance(value, dict):
            raise ExtensionHostProtocolError("extension response must be a JSON object")
        return value

    @staticmethod
    def _discard_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


__all__ = [
    "ExtensionHost",
    "ExtensionHostCrashed",
    "ExtensionHostError",
    "ExtensionHostLimits",
    "ExtensionHostOutputLimit",
    "ExtensionHostProtocolError",
    "ExtensionHostStats",
    "ExtensionHostTimeout",
]
