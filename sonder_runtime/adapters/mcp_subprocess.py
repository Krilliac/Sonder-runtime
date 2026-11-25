"""Bounded lifecycle ownership for one-shot stdio MCP providers."""
from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sonder_runtime.platform.logging import child_environment
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

from ..application.jobs.durable_registry import (
    ProcessTreeCleanupContract,
    ProcessTreeCleanupReceipt,
    ProcessTreeCleanupRequest,
)
from ..application.protocol.mcp_compatibility import LegacyMcpContract
from .process_termination import ProcessTreeSupervisor

_IO_CHUNK_BYTES = 8192
_DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_STDERR_BYTES = 256 * 1024
_DEFAULT_MAX_STDERR_DISCARD_BYTES = 16 * 1024 * 1024


class McpProviderTimeout(TimeoutError):
    """The provider exceeded its bounded stdio call or shutdown window."""


class McpProviderCancelled(InterruptedError):
    """The caller cancelled a bounded provider exchange."""


class McpProviderOutputLimit(ValueError):
    """The provider exceeded its bounded stdout or stderr byte limit."""

    is_output_limit = True


class McpProviderProtocolError(ValueError):
    """The provider emitted invalid UTF-8 on its MCP stdout stream."""


@dataclass(frozen=True)
class McpProviderLifecycleEvent:
    """Safe lifecycle observation; command and environment are never included."""

    state: str
    returncode: int | None = None
    cleanup_receipt: ProcessTreeCleanupReceipt | None = None


LifecycleObserver = Callable[[McpProviderLifecycleEvent], None]


def _validate_positive_number(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"MCP provider {name} must be a positive finite number")
    return float(value)


def _validate_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"MCP provider {name} must be a positive integer")
    return value


def _close_pipes(process: Any) -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, stream_name, None)
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class McpSubprocessProvider:
    """Run one MCP provider exchange with explicit, bounded ownership.

    The adapter is deliberately one-shot: a provider process handles one
    request stream and is always reaped before ``run`` returns or raises.
    Stdout and stderr are drained concurrently in bounded binary chunks to
    avoid stderr pipe deadlocks, locale-encoding corruption on Windows, and
    unbounded memory buffering.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
        shutdown_timeout_seconds: float = 2.0,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        max_stderr_bytes: int = _DEFAULT_MAX_STDERR_BYTES,
        max_stderr_discard_bytes: int = _DEFAULT_MAX_STDERR_DISCARD_BYTES,
        declaration: LegacyMcpContract | None = None,
        cleanup: ProcessTreeCleanupContract | None = None,
        platform_name: str | None = None,
        observer: LifecycleObserver | None = None,
        popen=subprocess.Popen,
    ) -> None:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("MCP provider argv must be non-empty strings")
        if env is not None and any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in env.items()
        ):
            raise ValueError("MCP provider environment must contain non-empty string pairs")
        self._timeout = _validate_positive_number(timeout_seconds, "timeouts")
        self._shutdown_timeout = _validate_positive_number(shutdown_timeout_seconds, "timeouts")
        self._max_output_bytes = _validate_positive_int(max_output_bytes, "max_output_bytes")
        self._max_stderr_bytes = _validate_positive_int(max_stderr_bytes, "max_stderr_bytes")
        self._max_stderr_discard_bytes = max(
            self._max_stderr_bytes,
            _validate_positive_int(max_stderr_discard_bytes, "max_stderr_discard_bytes"),
        )
        self._argv = tuple(argv)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        if declaration is not None and not isinstance(declaration, LegacyMcpContract):
            raise TypeError("MCP provider declaration must be a LegacyMcpContract")
        self._declaration = declaration
        self._platform = platform_name or os.name
        self._cleanup_provided = cleanup is not None
        self._cleanup = cleanup or ProcessTreeSupervisor(
            platform_name=self._platform, timeout_seconds=self._shutdown_timeout
        )
        self._observer = observer
        self._popen = popen
        self._state_lock = threading.RLock()
        self._active = False
        self._close_event = threading.Event()
        self._process: Any = None
        self._cleanup_receipt: ProcessTreeCleanupReceipt | None = None

    @property
    def declaration(self) -> LegacyMcpContract | None:
        return self._declaration

    @property
    def cleanup_receipt(self) -> ProcessTreeCleanupReceipt | None:
        """The last termination receipt, including an incomplete receipt."""
        return self._cleanup_receipt

    def run(
        self,
        request: str,
        *,
        cancel_check: Callable[[], bool] | None = None,
        deadline_monotonic: float | None = None,
        max_output_bytes: int | None = None,
        max_stderr_bytes: int | None = None,
    ) -> tuple[str, str]:
        """Exchange newline-delimited MCP input and return stdout/stderr."""
        if not isinstance(request, str):
            raise TypeError("MCP provider request must be text")
        try:
            request_bytes = request.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("MCP provider request must be valid UTF-8") from exc
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
        ):
            raise ValueError("MCP provider deadline_monotonic must be a finite number")
        effective_max_output = (
            self._max_output_bytes
            if max_output_bytes is None
            else _validate_positive_int(max_output_bytes, "max_output_bytes")
        )
        effective_max_stderr = (
            self._max_stderr_bytes
            if max_stderr_bytes is None
            else _validate_positive_int(max_stderr_bytes, "max_stderr_bytes")
        )
        effective_max_stderr_discard = max(
            effective_max_stderr, self._max_stderr_discard_bytes
        )
        if cancel_check is not None and cancel_check():
            raise McpProviderCancelled("MCP provider exchange was cancelled")
        if deadline_monotonic is not None and deadline_monotonic <= time.monotonic():
            raise McpProviderTimeout("MCP provider exceeded its bounded call deadline")

        with self._state_lock:
            if self._active or self._process is not None:
                raise RuntimeError("MCP provider exchange is already active")
            self._active = True
            self._close_event.clear()

        process: Any = None
        workers: list[threading.Thread] = []
        abort_event = threading.Event()
        termination_requested = False
        try:
            launch_env = self._env if self._env is not None else child_environment()
            process = self._popen(
                list(self._argv), cwd=self._cwd, env=launch_env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                **({"start_new_session": True} if self._platform == "posix" else {}),
                **(
                    {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                    if self._platform == "nt"
                    else {}
                ),
            )
            with self._state_lock:
                self._process = process
            self._emit("started")

            can_stream = (
                getattr(process, "stdout", None) is not None
                and getattr(process, "stderr", None) is not None
                and callable(getattr(process.stdout, "read", None))
                and callable(getattr(process.stderr, "read", None))
            )
            if can_stream:
                stdout, stderr, termination_requested = self._run_streaming(
                    process,
                    request_bytes,
                    workers=workers,
                    abort_event=abort_event,
                    cancel_check=cancel_check,
                    deadline_monotonic=deadline_monotonic,
                    max_output_bytes=effective_max_output,
                    max_stderr_bytes=effective_max_stderr,
                    max_stderr_discard_bytes=effective_max_stderr_discard,
                )
            else:
                stdout, stderr, termination_requested = self._run_communicate_fallback(
                    process,
                    request,
                    workers=workers,
                    cancel_check=cancel_check,
                    deadline_monotonic=deadline_monotonic,
                    max_output_bytes=effective_max_output,
                    max_stderr_bytes=effective_max_stderr,
                )
        except BaseException:
            abort_event.set()
            if process is not None and process.poll() is None and not termination_requested:
                termination_requested = True
                receipt = self._terminate_and_join(process, workers, "provider failure")
                self._emit("failed", process.returncode, receipt)
            raise
        finally:
            abort_event.set()
            if process is not None:
                if process.poll() is None and not termination_requested:
                    self._terminate_and_join(process, workers, "provider finalization")
                else:
                    self._join_workers(workers, self._shutdown_timeout)
                if not any(w.is_alive() for w in workers):
                    _close_pipes(process)
            with self._state_lock:
                self._process = None
                self._active = False
        self._emit("exited", process.returncode)
        return stdout, stderr

    def _run_streaming(
        self,
        process: Any,
        request_bytes: bytes,
        *,
        workers: list[threading.Thread],
        abort_event: threading.Event,
        cancel_check: Callable[[], bool] | None,
        deadline_monotonic: float | None,
        max_output_bytes: int,
        max_stderr_bytes: int,
        max_stderr_discard_bytes: int,
    ) -> tuple[str, str, bool]:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        io_errors: list[BaseException] = []
        output_exceeded = threading.Event()
        stderr_flood = threading.Event()
        worker_completions: list[tuple[str, threading.Thread, threading.Event]] = []

        stdout_done = threading.Event()
        stderr_done = threading.Event()
        stdin_done = threading.Event()

        def read_stdout() -> None:
            total = 0
            stream = process.stdout
            try:
                while not abort_event.is_set():
                    chunk = stream.read(_IO_CHUNK_BYTES)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    total += len(chunk)
                    if total > max_output_bytes:
                        output_exceeded.set()
                        abort_event.set()
                        break
                    stdout_chunks.append(chunk)
            except (OSError, ValueError) as exc:
                if not abort_event.is_set():
                    io_errors.append(exc)
            except BaseException as exc:
                io_errors.append(exc)
            finally:
                stdout_done.set()

        def read_stderr() -> None:
            kept = 0
            total_seen = 0
            stream = process.stderr
            try:
                while not abort_event.is_set():
                    chunk = stream.read(_IO_CHUNK_BYTES)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8", errors="replace")
                    total_seen += len(chunk)
                    if kept < max_stderr_bytes:
                        take = min(len(chunk), max_stderr_bytes - kept)
                        stderr_chunks.append(chunk[:take])
                        kept += take
                    if total_seen > max_stderr_discard_bytes:
                        stderr_flood.set()
                        abort_event.set()
                        break
            except (OSError, ValueError) as exc:
                if not abort_event.is_set():
                    io_errors.append(exc)
            except BaseException as exc:
                io_errors.append(exc)
            finally:
                stderr_done.set()

        def write_stdin() -> None:
            stream = getattr(process, "stdin", None)
            if stream is None:
                stdin_done.set()
                return
            view = memoryview(request_bytes)
            offset = 0
            try:
                while offset < len(view) and not abort_event.is_set():
                    end = min(offset + _IO_CHUNK_BYTES, len(view))
                    stream.write(view[offset:end])
                    offset = end
                if not abort_event.is_set():
                    flush = getattr(stream, "flush", None)
                    if callable(flush):
                        flush()
            except (BrokenPipeError, OSError, ValueError):
                # Child exited or closed stdin early; let stdout/stderr finish draining.
                pass
            except BaseException as exc:
                io_errors.append(exc)
            finally:
                try:
                    stream.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                stdin_done.set()

        try:
            stdout_worker = owned_runtime_thread(
                target=read_stdout, name="sonder-mcp-stdout", daemon=True
            )
            stdout_worker.start()
            workers.append(stdout_worker)
            worker_completions.append(("stdout", stdout_worker, stdout_done))

            stderr_worker = owned_runtime_thread(
                target=read_stderr, name="sonder-mcp-stderr", daemon=True
            )
            stderr_worker.start()
            workers.append(stderr_worker)
            worker_completions.append(("stderr", stderr_worker, stderr_done))

            if request_bytes:
                stdin_worker = owned_runtime_thread(
                    target=write_stdin, name="sonder-mcp-stdin", daemon=True
                )
                stdin_worker.start()
                workers.append(stdin_worker)
                worker_completions.append(("stdin", stdin_worker, stdin_done))
            else:
                stdin = getattr(process, "stdin", None)
                if stdin is not None:
                    try:
                        stdin.close()
                    except (BrokenPipeError, OSError, ValueError):
                        pass
        except BaseException:
            abort_event.set()
            receipt = self._terminate_and_join(process, workers, "provider worker refused")
            self._emit("failed", process.returncode, receipt)
            raise

        call_deadline = time.monotonic() + self._timeout
        if deadline_monotonic is not None:
            call_deadline = min(call_deadline, deadline_monotonic)

        while True:
            if self._close_event.is_set() or (cancel_check is not None and cancel_check()):
                abort_event.set()
                receipt = self._terminate_and_join(process, workers, "provider cancellation")
                self._emit("cancelled", process.returncode, receipt)
                raise McpProviderCancelled("MCP provider exchange was cancelled")
            if output_exceeded.is_set():
                abort_event.set()
                receipt = self._terminate_and_join(process, workers, "provider output limit")
                self._emit("failed", process.returncode, receipt)
                raise McpProviderOutputLimit("MCP provider stdout exceeded max_output_bytes")
            if stderr_flood.is_set():
                abort_event.set()
                receipt = self._terminate_and_join(process, workers, "provider stderr limit")
                self._emit("failed", process.returncode, receipt)
                raise McpProviderOutputLimit("MCP provider stderr exceeded max_stderr_discard_bytes")
            # Detect a managed runtime thread whose owner stopped between start()
            # and run(), skipping the target body without closing stdin or draining pipes.
            for label, worker, done_flag in worker_completions:
                if not worker.is_alive() and not done_flag.is_set():
                    abort_event.set()
                    receipt = self._terminate_and_join(
                        process, workers, f"provider {label} worker aborted"
                    )
                    self._emit("failed", process.returncode, receipt)
                    raise RuntimeError(f"MCP provider {label} worker terminated before running")
            remaining = call_deadline - time.monotonic()
            if remaining <= 0:
                abort_event.set()
                receipt = self._terminate_and_join(process, workers, "provider deadline")
                self._emit("timed_out", process.returncode, receipt)
                raise McpProviderTimeout("MCP provider exceeded its bounded call deadline")

            alive = [w for w in workers if w.is_alive()]
            slice_wait = min(remaining, 0.02)
            if alive:
                alive[0].join(slice_wait)
            else:
                if process.poll() is not None:
                    break
                try:
                    process.wait(timeout=slice_wait)
                    break
                except subprocess.TimeoutExpired:
                    pass

        if io_errors:
            raise io_errors[0]
        raw_stdout = b"".join(stdout_chunks)
        raw_stderr = b"".join(stderr_chunks)
        try:
            stdout_text = raw_stdout.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError as exc:
            raise McpProviderProtocolError("MCP provider stdout is not valid UTF-8") from exc
        stderr_text = raw_stderr.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return stdout_text, stderr_text, False

    def _run_communicate_fallback(
        self,
        process: Any,
        request: str,
        *,
        workers: list[threading.Thread],
        cancel_check: Callable[[], bool] | None,
        deadline_monotonic: float | None,
        max_output_bytes: int,
        max_stderr_bytes: int,
    ) -> tuple[str, str, bool]:
        result: list[object] = []

        def communicate() -> None:
            try:
                result.append(process.communicate(request))
            except BaseException as exc:
                result.append(exc)

        try:
            worker = owned_runtime_thread(
                target=communicate, name="sonder-mcp-provider", daemon=True
            )
            worker.start()
            workers.append(worker)
        except BaseException:
            receipt = self._terminate(process, "provider worker refused")
            self._emit("failed", process.returncode, receipt)
            raise

        call_deadline = time.monotonic() + self._timeout
        if deadline_monotonic is not None:
            call_deadline = min(call_deadline, deadline_monotonic)
        while True:
            if self._close_event.is_set() or (cancel_check is not None and cancel_check()):
                receipt = self._terminate_and_join(process, workers, "provider cancellation")
                self._emit("cancelled", process.returncode, receipt)
                raise McpProviderCancelled("MCP provider exchange was cancelled")
            remaining = call_deadline - time.monotonic()
            if remaining <= 0:
                receipt = self._terminate_and_join(process, workers, "provider deadline")
                self._emit("timed_out", process.returncode, receipt)
                raise McpProviderTimeout("MCP provider exceeded its bounded call deadline")
            worker.join(min(remaining, 0.05))
            if not worker.is_alive():
                break
        if not result:
            raise RuntimeError("MCP provider communication ended without a result")
        if isinstance(result[0], BaseException):
            raise result[0]
        raw_out, raw_err = result[0]  # type: ignore[misc]
        if isinstance(raw_out, bytes):
            if len(raw_out) > max_output_bytes:
                raise McpProviderOutputLimit("MCP provider stdout exceeded max_output_bytes")
            try:
                stdout_text = raw_out.decode("utf-8").replace("\r\n", "\n")
            except UnicodeDecodeError as exc:
                raise McpProviderProtocolError("MCP provider stdout is not valid UTF-8") from exc
        else:
            stdout_text = str(raw_out)
            if len(stdout_text.encode("utf-8", errors="replace")) > max_output_bytes:
                raise McpProviderOutputLimit("MCP provider stdout exceeded max_output_bytes")
        if isinstance(raw_err, bytes):
            stderr_text = raw_err[:max_stderr_bytes].decode("utf-8", errors="replace").replace("\r\n", "\n")
        else:
            stderr_text = str(raw_err)
            encoded_err = stderr_text.encode("utf-8", errors="replace")
            if len(encoded_err) > max_stderr_bytes:
                stderr_text = encoded_err[:max_stderr_bytes].decode("utf-8", errors="replace")
        return stdout_text, stderr_text, False

    def close(self) -> None:
        """Terminate and reap an active provider, if one exists."""
        with self._state_lock:
            process = self._process
            active = self._active
            if active:
                self._close_event.set()
        if process is None:
            return
        if active:
            # run() owns the I/O workers and reaping; signal and kill the
            # child without racing process.communicate().
            try:
                process.kill()
            except OSError:
                pass
            return
        if process.poll() is not None:
            _close_pipes(process)
            with self._state_lock:
                if self._process is process:
                    self._process = None
            return
        receipt = self._terminate(process, "provider closed")
        _close_pipes(process)
        self._emit("closed", process.returncode, receipt)
        with self._state_lock:
            if self._process is process:
                self._process = None

    def _join_workers(self, workers: Sequence[threading.Thread], timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        for worker in workers:
            if not worker.is_alive():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            worker.join(remaining)

    def _terminate_and_join(
        self,
        process: Any,
        workers: Sequence[threading.Thread],
        reason: str,
    ) -> ProcessTreeCleanupReceipt:
        receipt = self._terminate(process, reason, reap=False)
        first_wait = min(0.2, self._shutdown_timeout)
        self._join_workers(workers, first_wait)
        if any(w.is_alive() for w in workers):
            # A descendant may still hold stdout/stderr pipes open after the
            # direct child died. Escalate best-effort tree cleanup without
            # upgrading an incomplete receipt's truthfulness claim.
            if not self._cleanup_provided:
                request = ProcessTreeCleanupRequest(
                    job_id=f"mcp-provider:{getattr(process, 'pid', 0)}",
                    process_id=getattr(process, "pid", 0),
                    process_group_id=getattr(process, "pid", None) if self._platform == "posix" else None,
                    reason=reason,
                )
                try:
                    self._cleanup.cleanup(request)
                except Exception:
                    pass
            remaining = max(0.0, self._shutdown_timeout - first_wait)
            self._join_workers(workers, remaining)
        if process.poll() is None:
            try:
                process.wait(timeout=min(0.5, self._shutdown_timeout))
            except (subprocess.TimeoutExpired, OSError):
                pass
        return receipt

    def _terminate(self, process, reason: str, *, reap: bool = True) -> ProcessTreeCleanupReceipt:
        if not reap:
            # Deadline/cancellation owns a hard bound.  Stop the direct child
            # first; the receipt below still reports whether tree cleanup was
            # actually proven by the platform supervisor.
            try:
                process.kill()
            except OSError:
                pass
            if self._platform == "posix" and not self._cleanup_provided:
                pid = getattr(process, "pid", None)
                killpg = getattr(os, "killpg", None)
                sigkill = getattr(signal, "SIGKILL", None)
                if pid and callable(killpg) and sigkill is not None:
                    try:
                        killpg(pid, sigkill)
                    except (OSError, ProcessLookupError):
                        pass
        request = ProcessTreeCleanupRequest(
            job_id=f"mcp-provider:{getattr(process, 'pid', 0)}",
            process_id=getattr(process, "pid", 0),
            process_group_id=getattr(process, "pid", None) if self._platform == "posix" else None,
            reason=reason,
        )
        if not reap and not self._cleanup_provided:
            receipt = ProcessTreeCleanupReceipt(
                request.job_id, True, complete=False,
                detail="direct child stopped before bounded tree cleanup verification",
            )
        else:
            try:
                receipt = self._cleanup.cleanup(request)
            except Exception as exc:
                receipt = ProcessTreeCleanupReceipt(
                    request.job_id, True, complete=False,
                    detail=f"cleanup adapter failed: {type(exc).__name__}",
                )
        if not receipt.complete:
            # The receipt stays incomplete; this fallback is only a safety
            # attempt and never upgrades the claim made by the receipt.
            try:
                process.kill()
            except OSError:
                pass
        self._cleanup_receipt = receipt
        if reap:
            try:
                process.communicate(timeout=self._shutdown_timeout)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.communicate(timeout=self._shutdown_timeout)
                except subprocess.TimeoutExpired:
                    self._cleanup_receipt = ProcessTreeCleanupReceipt(
                        request.job_id, True, complete=False,
                        detail="provider did not reap before shutdown deadline",
                    )
        return self._cleanup_receipt

    def _emit(self, state: str, returncode: int | None = None, receipt=None) -> None:
        if self._observer is None:
            return
        try:
            self._observer(McpProviderLifecycleEvent(state, returncode, receipt))
        except Exception:
            pass


__all__ = [
    "McpProviderCancelled",
    "McpProviderLifecycleEvent",
    "McpProviderOutputLimit",
    "McpProviderProtocolError",
    "McpProviderTimeout",
    "McpSubprocessProvider",
]
